from dataclasses import dataclass
from .cc import RateCounter
from typing import Dict, List


@dataclass
class VideoTemporalLayerInfo:
    flow_id: int
    temporal_layer: int
    rate: RateCounter

    def __init__(self, flow_id: int, temporal_layer: int):
        self.flow_id = flow_id
        self.temporal_layer = temporal_layer
        self.rate = RateCounter()

    def str(self, now_ms: int):
        return "LayerInfo(id={}, lay={}, rate={})".format(
            self.flow_id, self.temporal_layer, self.rate.rate(now_ms))


AVAILABLE_BANDWIDTH_USAGE = 0.98
AVAILABLE_BANDWIDTH_BURST_USAGE = 1.1
DEFAULT_FRAME_SIZE_KB = 5  # kilobytes
LAYERS_PRINT_INTERVAL_MS = 1000


class TemporalLayerFilter:
    def __init__(self,
                 usage_coef=AVAILABLE_BANDWIDTH_USAGE,
                 burst_usage_coef=AVAILABLE_BANDWIDTH_BURST_USAGE):
        self.layers: Dict[int, Dict[int, VideoTemporalLayerInfo]] = dict()  # flow: {layer: info}
        self.ordered_layers: List[VideoTemporalLayerInfo] = list()
        self.tx_rate = RateCounter(window_size=2000)
        self.short_tx_rate = RateCounter(window_size=500)
        self.other_rate = RateCounter(window_size=2000)
        self.available_bitrate = None
        self.partial_passing_ = False
        self.last_print_ms = None
        self.usage_coef = usage_coef
        self.burst_usage_coef = burst_usage_coef

    def update_available_bitrate(self, bitrate: int):
        self.available_bitrate = bitrate

    def add_other_sample(self, data_bytes: int, now_ms: int):
        self.print_layers(now_ms)
        self.other_rate.add(data_bytes, now_ms)
        self.tx_rate.add(data_bytes, now_ms)
        self.short_tx_rate.add(data_bytes, now_ms)

    def add_video_sample(self, flow_id: int, layer: int, data_bytes: int, now_ms: int):
        self.print_layers(now_ms)

        if flow_id not in self.layers:
            self.layers[flow_id] = dict()
        flow_layers = self.layers[flow_id]
        if layer not in flow_layers:
            flow_layers[layer] = VideoTemporalLayerInfo(flow_id, layer)
            self.sort_layers()

        current = flow_layers[layer]
        current.rate.add(data_bytes, now_ms)

        current_layer_need = current.rate.rate(now_ms) or 0
        actual = self.tx_rate.rate(now_ms) or 0
        actual_short = self.short_tx_rate.rate(now_ms) or 0

        prior_to_current_layers_need = self.other_rate.rate(now_ms) or 0
        for prior_layer in self.ordered_layers:
            if prior_layer == current:
                break
            prior_to_current_layers_need += (prior_layer.rate.rate(now_ms) or 0)

        total_need = prior_to_current_layers_need + current_layer_need
        total_available = (self.available_bitrate or 0) * AVAILABLE_BANDWIDTH_USAGE

        do_pass = False
        do_partial_pass = False
        while True:
            if layer == 0:
                do_pass = True
                break
            if total_need <= total_available:
                do_pass = True
                break
            if total_available <= prior_to_current_layers_need:
                do_pass = False
                break
            # partial
            if self.usage_coef and actual + data_bytes * 8.0 > total_available * self.usage_coef:
                do_pass = False
                break
            if self.burst_usage_coef and actual_short + data_bytes * 8.0 > total_available * self.burst_usage_coef:
                do_pass = False
                break
            if actual + DEFAULT_FRAME_SIZE_KB * 8000 < total_available:
                do_pass = True
                do_partial_pass = True
                break
            break

        if do_pass:
            self.tx_rate.add(data_bytes, now_ms)
            self.short_tx_rate.add(data_bytes, now_ms)

        # print("++++++++++++++")
        # print("Avs, layer={}, pass={}, partial={}".format(layer, do_pass, do_partial_pass))
        # print("Avs, actual={}, short={}, layer current={}, prior={}, total={}, avail={:.1f}".format(
        #     actual, actual_short, current_layer_need, prior_to_current_layers_need, total_need, total_available))
        # print("-------------\n")

        return do_pass

    def sort_layers(self):
        self.ordered_layers = []
        for flow_id, layers in self.layers.items():
            for layer, info in layers.items():
                self.ordered_layers.append(info)

        self.ordered_layers.sort(key=lambda x: (x.flow_id, x.temporal_layer))

    def print_layers(self, now_ms: int):
        if self.last_print_ms is not None and now_ms - self.last_print_ms < LAYERS_PRINT_INTERVAL_MS:
            return
        self.last_print_ms = now_ms

        ss = ""
        for info in self.ordered_layers:
            ss += info.str(now_ms)
            ss += ", "
        # print("Print avail={} actual={}, short={}, layers=({})".format(
        #     self.available_bitrate, self.tx_rate.rate(now_ms), self.short_tx_rate.rate(now_ms), ss))
