import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator
import numpy as np
from collections import namedtuple
from policy import cc
from policy import svc
from policy import pacer
from typing import List
from transport import RtpPacket, clock
import statistics
import asyncio

Packet = namedtuple("Packet", ["now_ms", "layer", "size"])
packets: List[Packet] = []


def read_data():
    data_set = []
    fd = open("vp9-dump.csv")
    for line in fd:
        data = line.rstrip("\n").split(",")
        data_set.append(Packet(int(data[0]), int(data[1]), int(data[2])))
    return data_set


def cal_500ms_bitrate(data_set: List[Packet]):
    plt.xlabel("ms")
    plt.ylabel("mbps")

    window = 1000
    rates = [cc.RateCounter(window_size=window),  # 0
             cc.RateCounter(window_size=window),  # 0,1
             cc.RateCounter(window_size=window)]  # 0,1,2
    now_ms_arr = []
    t2 = []
    t1 = []
    t0 = []

    for pkt in data_set:
        if pkt.layer <= 2:
            rates[2].add(pkt.size, pkt.now_ms)
        if pkt.layer <= 1:
            rates[1].add(pkt.size, pkt.now_ms)
        if pkt.layer <= 0:
            rates[0].add(pkt.size, pkt.now_ms)
        now_ms_arr.append(pkt.now_ms)
        t0.append(rates[0].rate(pkt.now_ms))
        t1.append(rates[1].rate(pkt.now_ms))
        t2.append(rates[2].rate(pkt.now_ms))
        print(now_ms_arr[-1], t0[-1], t1[-1], t2[-1])

    for i in range(1000):
        now_ms_arr.pop(0)
        t0.pop(0)
        t1.pop(0)
        t2.pop(0)

    t0 = [x / 1e6 for x in t0]
    t1 = [x / 1e6 for x in t1]
    t2 = [x / 1e6 for x in t2]
    now_ms_arr = [x - now_ms_arr[0] for x in now_ms_arr]

    mean_val2 = statistics.fmean(t2)
    mean_val1 = statistics.fmean(t1)
    mean_val0 = statistics.fmean(t0)
    mean_line_x = [now_ms_arr[0], now_ms_arr[-1]]
    mean_line_y2 = [mean_val2, mean_val2]
    mean_line_y1 = [mean_val1, mean_val1]
    mean_line_y0 = [mean_val0, mean_val0]
    print("mean", mean_val2, mean_val1, mean_val0, mean_line_x)

    # display(now_ms_arr, t0, t1, t2)
    # _, subplot1 = plt.subplots()
    plt.axis(ymin=0, ymax=2.5)
    plt.plot(now_ms_arr, t0, linewidth=1, label="t0")
    plt.plot(now_ms_arr, t1, linewidth=1, label="t0+t1")
    plt.plot(now_ms_arr, t2, linewidth=1, label="t0+t1+t2")
    # plt.plot(mean_line_x, mean_line_y2, linewidth=1)
    # plt.plot(mean_line_x, mean_line_y1, linewidth=1)
    # plt.plot(mean_line_x, mean_line_y0, linewidth=1)

    # _, subplot2 = plt.subplots()
    # pl.axis(ymin=0, ymax=3.0)

    output = cc.RateCounter()
    fil = svc.TemporalLayerFilter()
    fil.update_available_bitrate(1_600_000)
    plt.plot(mean_line_x, [1.6, 1.6], linewidth=3, label="abw")
    ops = []
    for pkt in data_set:
        pas = fil.add_video_sample(0, pkt.layer, pkt.size, pkt.now_ms)
        if pas:
            output.add(pkt.size, pkt.now_ms)
            global packets
            packets.append(pkt)
        ops.append(output.rate(pkt.now_ms))
    for i in range(1000):
        ops.pop(0)
    ops = [x / 1e6 for x in ops]

    # plt.plot(mean_line_x, mean_line_y2, linewidth=1)
    # plt.plot(mean_line_x, mean_line_y1, linewidth=1)
    # plt.plot(mean_line_x, mean_line_y0, linewidth=1)

    plt.gca().margins(x=0)
    plt.gcf().canvas.draw()
    maxsize = 30
    m = 0.2  # inch margin
    s = maxsize / plt.gcf().dpi * 60 + 2 * m
    margin = m / plt.gcf().get_size_inches()[0]

    plt.gcf().subplots_adjust(left=margin, right=1. - margin)
    plt.gcf().set_size_inches(s, plt.gcf().get_size_inches()[1])
    plt.gca().xaxis.set_major_locator(MultipleLocator(5000))

    plt.legend(loc="lower right")
    plt.savefig("1.png", dpi=600)

    plt.plot(now_ms_arr, ops, linewidth=1, label="svc_output")
    plt.legend(loc="lower right")
    plt.savefig("2.png", dpi=600)
    # plt.show()


async def display_pacing():
    global packets
    pac = pacer.Pacer()
    pac.update_bitrate(1_600_000)
    running = True

    async def write_loop():
        start_time = packets[0].now_ms
        start_now = clock.current_ms()
        while len(packets):
            pkt = packets[0]
            now = clock.current_ms()
            diff = (now - start_now) - (pkt.now_ms - start_time)
            if diff >= 0:
                packets.pop(0)
                print("write pkt", pkt)
                pac.enqueue(RtpPacket(timestamp=pkt.now_ms, payload='0' * pkt.size))
            else:
                await asyncio.sleep(-diff / 1000.0)
        nonlocal running
        running = False
        print("write end")

    now_ms_arr = []
    pb = []
    rate = cc.RateCounter(window_size=1000)

    async def read_loop():
        while running:
            pkt = await pac.read_queue()
            now_ms = clock.current_ms()
            if pkt:
                print("read pkt", pkt)
                rate.add(len(pkt.payload), now_ms)
                br = rate.rate(now_ms)
                if br:
                    now_ms_arr.append(now_ms)
                    pb.append(br)

    start_time = clock.current_ms()
    asyncio.ensure_future(write_loop())
    await read_loop()
    print("Duration: ", clock.current_ms() - start_time)
    print("Frames Duration: ", now_ms_arr[-1] - now_ms_arr[0])
    for i in range(500):
        now_ms_arr.pop(0)
        now_ms_arr.pop(-1)
        pb.pop(0)
        pb.pop(-1)

    pb = [x / 1e6 for x in pb]
    now_ms_arr = [x - now_ms_arr[0] for x in now_ms_arr]

    plt.plot(now_ms_arr, pb, color="gold", linewidth=1, label="pacer_output")

    plt.legend(loc="lower right")
    plt.savefig("3.png", dpi=600)
    plt.show()


# def display(*args):
#     plt.plot(args)
#     plt.show()


if __name__ == "__main__":
    cal_500ms_bitrate(read_data())
    asyncio.run(display_pacing())
    # display()
