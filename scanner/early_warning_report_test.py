"""Temporary 15-minute Telegram report for the Early Warning final POC test.

Tracks only ZKC and OPG, which were automatically selected by the VPD-derived
watchlist and entered ORANGE in the first live Early Warning run. This reporter
does not alter VPD v1.4 or Early Warning scoring/state logic.
"""

from datetime import datetime

from early_warning_watch import (
    KST,
    analyse_intraday,
    read_public_json,
    select_watchlist,
    send_telegram,
)

TEST_COINS = ("ZKC", "OPG")


def fmt(v, suffix=""):
    if v is None:
        return "-"
    return f"{v}{suffix}"


def level_emoji(level):
    return {
        "RED": "🔴",
        "ORANGE": "🟠",
        "WATCH": "🟡",
        "QUIET": "⚪",
    }.get(level, "⚪")


def interpretation(x):
    level = x.get("EWLevel")
    p15 = abs(float(x.get("Price15m%") or 0))
    a5 = float(x.get("ValueAccel5m") or 0)
    a15 = float(x.get("ValueAccel15m") or 0)

    if level == "RED":
        return "강한 단기 수급가속 — 추격 여부보다 지속성 확인"
    if level == "ORANGE":
        if p15 <= 2:
            return "수급 선행 / 가격 반응 제한"
        if p15 <= 4:
            return "수급가속 + 가격 반응 시작"
        return "수급가속 / 가격도 이미 반응 중"
    if level == "WATCH":
        if a5 >= 1.1 or a15 >= 1.1:
            return "가속 조짐 있으나 경보 기준 미달"
        return "관찰 유지 / 단기 가속 약함"
    return "단기 수급가속 없음"


def main():
    snapshot = read_public_json("vpd_latest.json")
    if not snapshot:
        raise RuntimeError("vpd_latest.json unavailable")

    watchlist = select_watchlist(snapshot)
    by_coin = {x["coin"]: x for x in watchlist}

    rows = []
    for coin in TEST_COINS:
        item = by_coin.get(coin)
        if not item:
            rows.append(f"⚪ {coin} | 현재 VPD watchlist 이탈")
            continue
        obs = analyse_intraday(item)
        if not obs:
            rows.append(f"⚪ {coin} | 분봉 데이터 분석 실패")
            continue

        rows.append(
            f"{level_emoji(obs['EWLevel'])} {coin} | {obs['EWLevel']} {obs['EWScore']} | VPD {obs['VPD']:.0f} | Mom {obs.get('momentum','-')}\n"
            f"   5m ValueAccel {fmt(obs.get('ValueAccel5m'), 'x')} | Price {fmt(obs.get('Price5m%'), '%')}\n"
            f"   15m ValueAccel {fmt(obs.get('ValueAccel15m'), 'x')} | Price {fmt(obs.get('Price15m%'), '%')}\n"
            f"   판정: {interpretation(obs)}"
        )

    now = datetime.now(KST).strftime("%H:%M")
    vpd_asof = snapshot.get("asof_kst") or snapshot.get("asof") or "-"
    text = (
        f"🧪 VPD EARLY WARNING 15M FINAL TEST | {now}\n"
        f"VPD 기준 {vpd_asof}\n\n"
        + "\n\n".join(rows)
        + "\n\n※ 테스트 리포트: ZKC·OPG만 15분마다 추적 / VPD 점수와 EW 기준은 변경하지 않음"
    )
    send_telegram(text)


if __name__ == "__main__":
    main()
