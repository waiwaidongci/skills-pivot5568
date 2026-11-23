"""两期榆桥培训中心的虚拟受理记录和确认回执。"""

from .spec import CHANNELS, PROGRAMS


# 顺序与公开名册一致，每格为去重受理数、确认报名数。
NOVEMBER = (
    ((20, 12), (11, 5), (5, 4)),
    ((18, 8), (14, 5), (13, 7)),
    ((12, 6), (15, 9), (10, 5)),
)
OCTOBER = (((20, 12), (11, 5), (12, 5)), *NOVEMBER[1:])


def _month_rows(period, grid):
    rows = []
    for channel_index, channel in enumerate(CHANNELS):
        for program_index, program in enumerate(PROGRAMS):
            total, confirmed = grid[channel_index][program_index]
            for index in range(total):
                intake = {
                    "learner_id": f"YQ-{channel_index + 1}{program_index + 1}-{index + 1:03d}",
                    "intake_date": f"{period}-06",
                    "channel": channel,
                    "program": program,
                    "enrolled": False,
                }
                rows.append(intake)
                if index < confirmed:
                    rows.append({**intake, "intake_date": f"{period}-18", "enrolled": True})
                if index == 0:
                    rows.append(dict(intake))
                    rows.append({**intake, "intake_date": f"{period}-20",
                                 "channel": CHANNELS[(channel_index + 1) % len(CHANNELS)]})
    return rows


def demo_rows() -> list[dict]:
    return [*_month_rows("2026-10", OCTOBER), *_month_rows("2026-11", NOVEMBER)]


def channel_month_rows(period, channel, grid=None) -> list[dict]:
    """单个渠道的整月批次内容，演示各渠道分别上传整月批次。"""
    return [row for row in _month_rows(period, NOVEMBER if grid is None else grid)
            if row["channel"] == channel]
