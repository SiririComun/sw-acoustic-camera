"""Provisional two-source heatmaps on BGR camera frames.

Projection uses a configurable camera field of view. It is a display model,
not a physically calibrated registration of microphone ports to camera pixels.
"""

import math

import numpy as np


SOURCE_COLORS_BGR = ((0, 0, 255), (255, 190, 0))


class TwoSourceVisualState:
    """Keep the latest valid AoA per active detector ID for a bounded time."""

    def __init__(self, max_age_s=0.5):
        if not math.isfinite(max_age_s) or max_age_s <= 0:
            raise ValueError('max_age_s must be finite and positive')
        self.max_age_s = float(max_age_s)
        self.clear()

    def clear(self):
        self._latest = {}

    def update(self, pipeline_row, now_s):
        if not math.isfinite(now_s):
            raise ValueError('now_s must be finite')
        for event in pipeline_row['window']['events']:
            if event['type'] in ('start', 'end'):
                self._latest.pop(event['id'], None)
        for source in pipeline_row['sources']:
            source_id = source['id']
            if not source['present']:
                self._latest.pop(source_id, None)
                continue
            aoa = source['aoa']
            if aoa is None:
                continue  # A skipped fit may use a recent valid result.
            if aoa['status'] == 'VALID_PROVISIONAL' and aoa['direction'] is not None:
                self._latest[source_id] = (float(now_s), {
                    'id': source_id,
                    'frequency_hz': float(source['frequency_hz']),
                    'azimuth_deg': float(aoa['azimuth_deg']),
                    'elevation_deg': float(aoa['elevation_deg']),
                    'status': aoa['status'],
                    'first_frame': aoa['first_frame'],
                })
            else:
                self._latest.pop(source_id, None)

    def snapshot(self, now_s):
        if not math.isfinite(now_s):
            raise ValueError('now_s must be finite')
        expired = [source_id for source_id, (timestamp, _) in self._latest.items()
                   if now_s < timestamp or now_s - timestamp > self.max_age_s]
        for source_id in expired:
            self._latest.pop(source_id, None)
        return [dict(self._latest[source_id][1]) for source_id in sorted(self._latest)]


def direction_pixel(azimuth_deg, elevation_deg, width, height,
                    hfov_deg=70.0, vfov_deg=50.0):
    """Use the same provisional array-to-camera projection as Phase 11."""
    if (not all(math.isfinite(value) for value in
                (azimuth_deg, elevation_deg, hfov_deg, vfov_deg))
            or not 1 < hfov_deg < 170 or not 1 < vfov_deg < 170
            or width <= 0 or height <= 0):
        raise ValueError('invalid direction, field of view, or frame dimensions')
    x = width * (0.5 - math.tan(math.radians(azimuth_deg))
                 / (2 * math.tan(math.radians(hfov_deg / 2))))
    y = height * (0.5 - math.tan(math.radians(elevation_deg))
                  / (2 * math.tan(math.radians(vfov_deg / 2))))
    return int(round(x)), int(round(y))


def render_two_source_maps(frame_bgr, estimates, *, hfov_deg=70.0,
                           vfov_deg=50.0, sigma_px=24.0, alpha=0.65,
                           include_individual=True, include_maps=True):
    """Return two sparse heatmaps, a combined map and composited BGR frames.

    ``estimates`` contains recent valid snapshots from TwoSourceVisualState.
    Invalid statuses and off-frame projections make no layer. The input frame
    is never changed. The live path can omit individual frames and full-size
    diagnostic maps; compositing touches only the small Gaussian regions.
    """
    frame = np.asarray(frame_bgr)
    if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError('frame_bgr must be uint8 with shape (H,W,3)')
    if (not math.isfinite(sigma_px) or sigma_px <= 0
            or not math.isfinite(alpha) or not 0 <= alpha <= 1):
        raise ValueError('sigma_px and alpha are invalid')
    height, width = frame.shape[:2]
    if not height or not width:
        raise ValueError('frame dimensions must be positive')
    estimates = list(estimates)
    if len(estimates) > 2 or len({item['id'] for item in estimates}) != len(estimates):
        raise ValueError('expected at most two distinct source IDs')
    maps = {}
    combined_map = np.zeros((height, width), dtype=np.float32) if include_maps else None
    regions = []
    visible = []
    radius = max(1, int(math.ceil(3 * sigma_px)))
    for item in estimates:
        source_id = item['id']
        if source_id not in (0, 1) or item.get('status') != 'VALID_PROVISIONAL':
            continue
        pixel = direction_pixel(item['azimuth_deg'], item['elevation_deg'],
                                width, height, hfov_deg, vfov_deg)
        x, y = pixel
        if not 0 <= x < width or not 0 <= y < height:
            continue
        left, right = max(0, x - radius), min(width, x + radius + 1)
        top, bottom = max(0, y - radius), min(height, y + radius + 1)
        xx = np.arange(left, right, dtype=np.float32) - x
        yy = np.arange(top, bottom, dtype=np.float32) - y
        heat = np.exp(
            -(yy[:, None] ** 2 + xx[None, :] ** 2) / (2 * sigma_px ** 2))
        regions.append((source_id, left, right, top, bottom, heat))
        if include_maps:
            source_map = np.zeros((height, width), dtype=np.float32)
            source_map[top:bottom, left:right] = heat
            maps[source_id] = source_map
            combined_map[top:bottom, left:right] = np.maximum(
                combined_map[top:bottom, left:right], heat)
        visible.append({'id': source_id, 'frequency_hz': item['frequency_hz'],
                        'pixel': pixel, 'status': item['status']})

    individual = {}
    combined_frame = frame.copy()
    for source_id, left, right, top, bottom, heat in regions:
        color = np.asarray(SOURCE_COLORS_BGR[source_id], dtype=np.float32)
        weight = (alpha * heat)[:, :, None]
        region = combined_frame[top:bottom, left:right].astype(np.float32)
        combined_frame[top:bottom, left:right] = np.rint(
            region * (1 - weight) + color * weight).astype(np.uint8)
        if include_individual:
            source_frame = frame.copy()
            source_region = source_frame[top:bottom, left:right].astype(np.float32)
            source_frame[top:bottom, left:right] = np.rint(
                source_region * (1 - weight) + color * weight).astype(np.uint8)
            individual[source_id] = source_frame
    return {'source_maps': maps, 'combined_map': combined_map,
            'source_frames': individual, 'combined_frame': combined_frame,
            'visible_sources': visible}
