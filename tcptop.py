#!/usr/bin/env python
# @lint-avoid-python-3-compatibility-imports
#
# tcptop    Summarize TCP send/recv throughput by host.
#           For Linux, uses BCC, eBPF. Embedded C.
#
# USAGE: tcptop [-h] [-C] [-S] [-p PID] [interval [count]] [-4 | -6]
#
# This uses dynamic tracing of kernel functions, and will need to be updated
# to match kernel changes.
#
# WARNING: This traces all send/receives at the TCP level, and while it
# summarizes data in-kernel to reduce overhead, there may still be some
# overhead at high TCP send/receive rates (eg, ~13% of one CPU at 100k TCP
# events/sec. This is not the same as packet rate: funccount can be used to
# count the kprobes below to find out the TCP rate). Test in a lab environment
# first. If your send/receive rate is low (eg, <1k/sec) then the overhead is
# expected to be negligible.
#
# ToDo: Fit output to screen size (top X only) in default (not -C) mode.
#
# Copyright 2016 Netflix, Inc.
# Licensed under the Apache License, Version 2.0 (the "License")
#
# 02-Sep-2016   Brendan Gregg   Created this.

from __future__ import print_function
from bcc import BPF
from bcc.containers import filter_by_containers
import argparse
from socket import inet_ntop, AF_INET, AF_INET6
from struct import pack
from time import sleep, strftime
from subprocess import call
from collections import namedtuple, defaultdict
from apscheduler.schedulers.blocking import BlockingScheduler
import time
import numpy as np

# arguments
def range_check(string):
    value = int(string)
    if value < 1:
        msg = "value must be stricly positive, got %d" % (value,)
        raise argparse.ArgumentTypeError(msg)
    return value

examples = """examples:
    ./tcptop           # trace TCP send/recv by host
    ./tcptop -C        # don't clear the screen
    ./tcptop -p 181    # only trace PID 181
    ./tcptop --cgroupmap mappath  # only trace cgroups in this BPF map
    ./tcptop --mntnsmap mappath   # only trace mount namespaces in the map
    ./tcptop -4        # trace IPv4 family only
    ./tcptop -6        # trace IPv6 family only
"""
parser = argparse.ArgumentParser(
    description="Summarize TCP send/recv throughput by host",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=examples)
parser.add_argument("-C", "--noclear", action="store_true",
    help="don't clear the screen")
parser.add_argument("-S", "--nosummary", action="store_true",
    help="skip system summary line")
parser.add_argument("-p", "--pid",
    help="trace this PID only")
parser.add_argument("interval", nargs="?", default=1, type=range_check,
    help="output interval, in seconds (default 1)")
parser.add_argument("count", nargs="?", default=-1, type=range_check,
    help="number of outputs")
parser.add_argument("--cgroupmap",
    help="trace cgroups in this BPF map only")
parser.add_argument("--mntnsmap",
    help="trace mount namespaces in this BPF map only")
group = parser.add_mutually_exclusive_group()
group.add_argument("-4", "--ipv4", action="store_true",
    help="trace IPv4 family only")
group.add_argument("-6", "--ipv6", action="store_true",
    help="trace IPv6 family only")
parser.add_argument("--ebpf", action="store_true",
    help=argparse.SUPPRESS)
args = parser.parse_args()
debug = 0

# linux stats
loadavg = "/proc/loadavg"

# define BPF program
bpf_text = """
#include <uapi/linux/ptrace.h>
#include <net/sock.h>
#include <bcc/proto.h>

struct ipv4_key_t {
    u32 pid;
    char name[TASK_COMM_LEN];
    u32 saddr;
    u32 daddr;
    u16 lport;
    u16 dport;
};
BPF_HASH(ipv4_send_bytes, struct ipv4_key_t);
BPF_HASH(ipv4_recv_bytes, struct ipv4_key_t);

struct ipv6_key_t {
    unsigned __int128 saddr;
    unsigned __int128 daddr;
    u32 pid;
    char name[TASK_COMM_LEN];
    u16 lport;
    u16 dport;
    u64 __pad__;
};
BPF_HASH(ipv6_send_bytes, struct ipv6_key_t);
BPF_HASH(ipv6_recv_bytes, struct ipv6_key_t);
BPF_HASH(sock_store, u32, struct sock *);

static int tcp_sendstat(int size)
{
    if (container_should_be_filtered()) {
        return 0;
    }

    u32 pid = bpf_get_current_pid_tgid() >> 32;
    FILTER_PID
    u32 tid = bpf_get_current_pid_tgid();
    struct sock **sockpp;
    sockpp = sock_store.lookup(&tid);
    if (sockpp == 0) {
        return 0; //miss the entry
    }
    struct sock *sk = *sockpp;
    u16 dport = 0, family;
    bpf_probe_read_kernel(&family, sizeof(family),
        &sk->__sk_common.skc_family);
    FILTER_FAMILY
    
    if (family == AF_INET) {
        struct ipv4_key_t ipv4_key = {.pid = pid};
        bpf_get_current_comm(&ipv4_key.name, sizeof(ipv4_key.name));
        bpf_probe_read_kernel(&ipv4_key.saddr, sizeof(ipv4_key.saddr),
            &sk->__sk_common.skc_rcv_saddr);
        bpf_probe_read_kernel(&ipv4_key.daddr, sizeof(ipv4_key.daddr),
            &sk->__sk_common.skc_daddr);
        bpf_probe_read_kernel(&ipv4_key.lport, sizeof(ipv4_key.lport),
            &sk->__sk_common.skc_num);
        bpf_probe_read_kernel(&dport, sizeof(dport),
            &sk->__sk_common.skc_dport);
        ipv4_key.dport = ntohs(dport);
        ipv4_send_bytes.increment(ipv4_key, size);

    } else if (family == AF_INET6) {
        struct ipv6_key_t ipv6_key = {.pid = pid};
        bpf_get_current_comm(&ipv6_key.name, sizeof(ipv6_key.name));
        bpf_probe_read_kernel(&ipv6_key.saddr, sizeof(ipv6_key.saddr),
            &sk->__sk_common.skc_v6_rcv_saddr.in6_u.u6_addr32);
        bpf_probe_read_kernel(&ipv6_key.daddr, sizeof(ipv6_key.daddr),
            &sk->__sk_common.skc_v6_daddr.in6_u.u6_addr32);
        bpf_probe_read_kernel(&ipv6_key.lport, sizeof(ipv6_key.lport),
            &sk->__sk_common.skc_num);
        bpf_probe_read_kernel(&dport, sizeof(dport),
            &sk->__sk_common.skc_dport);
        ipv6_key.dport = ntohs(dport);
        ipv6_send_bytes.increment(ipv6_key, size);
    }
    sock_store.delete(&tid);
    // else drop

    return 0;
}

int kretprobe__tcp_sendmsg(struct pt_regs *ctx)
{
    int size = PT_REGS_RC(ctx);
    if (size > 0)
        return tcp_sendstat(size);
    else
        return 0;
}

int kretprobe__tcp_sendpage(struct pt_regs *ctx)
{
    int size = PT_REGS_RC(ctx);
    if (size > 0)
        return tcp_sendstat(size);
    else
        return 0;
}

static int tcp_send_entry(struct sock *sk)
{
    if (container_should_be_filtered()) {
        return 0;
    }
    u32 pid = bpf_get_current_pid_tgid() >> 32;
    FILTER_PID
    u32 tid = bpf_get_current_pid_tgid();
    u16 family = sk->__sk_common.skc_family;
    FILTER_FAMILY
    sock_store.update(&tid, &sk);
    return 0;
}

int kprobe__tcp_sendmsg(struct pt_regs *ctx, struct sock *sk,
    struct msghdr *msg, size_t size)
{
    return tcp_send_entry(sk);
}

int kprobe__tcp_sendpage(struct pt_regs *ctx, struct sock *sk,
    struct page *page, int offset, size_t size)
{
    return tcp_send_entry(sk);
}
/*
 * tcp_recvmsg() would be obvious to trace, but is less suitable because:
 * - we'd need to trace both entry and return, to have both sock and size
 * - misses tcp_read_sock() traffic
 * we'd much prefer tracepoints once they are available.
 */
int kprobe__tcp_cleanup_rbuf(struct pt_regs *ctx, struct sock *sk, int copied)
{
    if (container_should_be_filtered()) {
        return 0;
    }

    u32 pid = bpf_get_current_pid_tgid() >> 32;
    FILTER_PID

    u16 dport = 0, family = sk->__sk_common.skc_family;
    u64 *val, zero = 0;

    if (copied <= 0)
        return 0;

    FILTER_FAMILY
    
    if (family == AF_INET) {
        struct ipv4_key_t ipv4_key = {.pid = pid};
        bpf_get_current_comm(&ipv4_key.name, sizeof(ipv4_key.name));
        ipv4_key.saddr = sk->__sk_common.skc_rcv_saddr;
        ipv4_key.daddr = sk->__sk_common.skc_daddr;
        ipv4_key.lport = sk->__sk_common.skc_num;
        dport = sk->__sk_common.skc_dport;
        ipv4_key.dport = ntohs(dport);
        ipv4_recv_bytes.increment(ipv4_key, copied);

    } else if (family == AF_INET6) {
        struct ipv6_key_t ipv6_key = {.pid = pid};
        bpf_get_current_comm(&ipv6_key.name, sizeof(ipv6_key.name));
        bpf_probe_read_kernel(&ipv6_key.saddr, sizeof(ipv6_key.saddr),
            &sk->__sk_common.skc_v6_rcv_saddr.in6_u.u6_addr32);
        bpf_probe_read_kernel(&ipv6_key.daddr, sizeof(ipv6_key.daddr),
            &sk->__sk_common.skc_v6_daddr.in6_u.u6_addr32);
        ipv6_key.lport = sk->__sk_common.skc_num;
        dport = sk->__sk_common.skc_dport;
        ipv6_key.dport = ntohs(dport);
        ipv6_recv_bytes.increment(ipv6_key, copied);
    }
    // else drop

    return 0;
}
"""

# code substitutions
if args.pid:
    bpf_text = bpf_text.replace('FILTER_PID',
        'if (pid != %s) { return 0; }' % args.pid)
else:
    bpf_text = bpf_text.replace('FILTER_PID', '')
if args.ipv4:
    bpf_text = bpf_text.replace('FILTER_FAMILY',
        'if (family != AF_INET) { return 0; }')
elif args.ipv6:
    bpf_text = bpf_text.replace('FILTER_FAMILY',
        'if (family != AF_INET6) { return 0; }')
bpf_text = bpf_text.replace('FILTER_FAMILY', '')
bpf_text = filter_by_containers(args) + bpf_text
if debug or args.ebpf:
    print(bpf_text)
    if args.ebpf:
        exit()

TCPSessionKey = namedtuple('TCPSession', ['pid', 'name', 'laddr', 'lport', 'daddr', 'dport'])

def get_ipv4_session_key(k):
    return TCPSessionKey(pid=k.pid,
                         name=k.name,
                         laddr=inet_ntop(AF_INET, pack("I", k.saddr)),
                         lport=k.lport,
                         daddr=inet_ntop(AF_INET, pack("I", k.daddr)),
                         dport=k.dport)

def get_ipv6_session_key(k):
    return TCPSessionKey(pid=k.pid,
                         name=k.name,
                         laddr=inet_ntop(AF_INET6, k.saddr),
                         lport=k.lport,
                         daddr=inet_ntop(AF_INET6, k.daddr),
                         dport=k.dport)

# initialize BPF
b = BPF(text=bpf_text)

ipv4_send_bytes = b["ipv4_send_bytes"]
ipv4_recv_bytes = b["ipv4_recv_bytes"]
ipv6_send_bytes = b["ipv6_send_bytes"]
ipv6_recv_bytes = b["ipv6_recv_bytes"]

print('Tracing... Output every %s secs. Hit Ctrl-C to end' % args.interval)

throughput_list1 = []
throughput_list2 = []
output_list = ['../throughput_output/scene1_new.txt', '../throughput_output/scene2_new.txt', '../throughput_output/scene3_new.txt']
current_timestamp = 0

def writeThroughput():
    with open(output_list[2], 'a') as f:
    # with open('../throughput_output/total1.txt', 'a') as f:
        # print("open path")
        try:
            writeData = np.array(throughput_list1)
            np.savetxt(f, writeData, delimiter=" ")
            throughput_list1.clear()
        except Exception as e:
            print(e.message)
    # with open('../throughput_output/total2.txt', 'a') as f:
    #     # print("open path")
    #     try:
    #         writeData = np.array(throughput_list2)
    #         np.savetxt(f, writeData, delimiter=" ")
    #         throughput_list2.clear()
    #     except Exception as e:
    #         print(e.message)
                

# scheduler = BlockingScheduler()
# scheduler.add_job(writeThroughput, 'interval', seconds=3, id='createData')
# scheduler.start()
# output
i = 0
exiting = False
print("start")
while i != args.count and not exiting:
    try:
        sleep(args.interval)
    except KeyboardInterrupt:
        exiting = True

    # header
    if args.noclear:
        print()
    else:
        call("clear")
    if not args.nosummary:
        with open(loadavg) as stats:
            print("%-8s loadavg: %s" % (strftime("%H:%M:%S"), stats.read()))

    # IPv4: build dict of all seen keys
    ipv4_throughput = defaultdict(lambda: [0, 0])
    for k, v in ipv4_send_bytes.items():
        key = get_ipv4_session_key(k)
        ipv4_throughput[key][0] = v.value
    ipv4_send_bytes.clear()

    for k, v in ipv4_recv_bytes.items():
        key = get_ipv4_session_key(k)
        ipv4_throughput[key][1] = v.value
    ipv4_recv_bytes.clear()

    if ipv4_throughput:
        print("%-7s %-12s %-21s %-21s %6s %6s" % ("PID", "COMM",
            "LADDR", "RADDR", "RX_KB", "TX_KB"))

    # output
    for k, (send_bytes, recv_bytes) in sorted(ipv4_throughput.items(),
                                              key=lambda kv: sum(kv[1]),
                                              reverse=True):
        recv_rate, send_rate = int(recv_bytes / 1024), int(send_bytes / 1024)
        print("%-7d %-12.12s %-21s %-21s %6d %6d" % (k.pid,
            k.name,
            k.laddr + ":" + str(k.lport),
            k.daddr + ":" + str(k.dport),
            recv_rate, send_rate))
        if str(k.dport) == "12345":
            throughput_list1.append(recv_rate)
        # elif str(k.dport) == "12346":
        #     throughput_list2.append(recv_rate)
            
            if time.time() - current_timestamp >= 5:
                writeThroughput()
                current_timestamp = time.time()

    # IPv6: build dict of all seen keys
    ipv6_throughput = defaultdict(lambda: [0, 0])
    for k, v in ipv6_send_bytes.items():
        key = get_ipv6_session_key(k)
        ipv6_throughput[key][0] = v.value
    ipv6_send_bytes.clear()

    for k, v in ipv6_recv_bytes.items():
        key = get_ipv6_session_key(k)
        ipv6_throughput[key][1] = v.value
    ipv6_recv_bytes.clear()

    if ipv6_throughput:
        # more than 80 chars, sadly.
        print("\n%-7s %-12s %-32s %-32s %6s %6s" % ("PID", "COMM",
            "LADDR6", "RADDR6", "RX_KB", "TX_KB"))

    # output
    for k, (send_bytes, recv_bytes) in sorted(ipv6_throughput.items(),
                                              key=lambda kv: sum(kv[1]),
                                              reverse=True):
        print("%-7d %-12.12s %-32s %-32s %6d %6d" % (k.pid,
            k.name,
            k.laddr + ":" + str(k.lport),
            k.daddr + ":" + str(k.dport),
            int(recv_bytes / 1024), int(send_bytes / 1024)))

    i += 1