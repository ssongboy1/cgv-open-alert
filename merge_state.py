#!/usr/bin/env python3
"""겹쳐 실행된 워크플로의 state.json 을 병합한다.

    python merge_state.py <우리것.json> <원격것.json>
    -> 원격것.json 자리에 병합 결과를 쓴다.

왜 필요한가
-----------
cron-job.org 트리거와 시간별 안전망 cron 이 겹치면 두 실행이 각각
state.json 을 고친 뒤 동시에 push 하려 한다. git rebase 는 이걸
자동으로 못 풀고 충돌로 죽는다.

단순히 한쪽을 버리면 안 된다. 먼저 끝난 실행이 알림을 보내고 seen 에
넣어둔 회차를, 나중 실행이 모르는 상태로 덮어쓰면 그 회차를 다시
알리게 된다. 그래서 seen 은 반드시 합집합으로 합친다.

다만 합집합만 하면 '지우는 일'이 영원히 일어나지 않는다. 한쪽이 지운
것을 다른 쪽이 매번 되살리기 때문이다. 실제로 그래서
  * watcher 가 버린 오래된 회차 키가 되살아나 state.json 이 계속 커졌고,
  * 삭제한 감시 대상의 기준선 기록이 남아 있었다.
그래서 합친 뒤에 watcher 와 같은 기준으로 한 번 더 거른다.
"""

import json
import sys

import watcher


def valid_target_ids(config_path="config.json"):
    """config 에 실제로 남아 있는 감시 대상의 id.

    읽지 못하면 None. 그 경우 기준선을 거르지 않는다. 잘못 걸러서
    잃는 쪽이, 지워진 기록이 남는 쪽보다 나쁘기 때문이다.
    """
    try:
        with open(config_path, encoding="utf-8") as fp:
            targets = json.load(fp)["targets"]
        return {watcher.target_id(t) for t in targets}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def merge(ours, theirs, target_ids=None):
    skip = ("seen", "gates", "baselined")
    out = dict(theirs)
    out.update({k: v for k, v in ours.items() if k not in skip})

    # 알림을 보낸 기록은 어느 쪽도 잃으면 안 된다. 합집합.
    # 합친 뒤 오래된 회차 키는 버린다. 이게 없으면 watcher 가 버린 키가
    # 원격에서 매번 되살아나 state.json 이 무한히 커진다.
    out["seen"] = sorted(watcher.prune_seen(
        set(ours.get("seen", [])) | set(theirs.get("seen", []))))

    # 기준선을 잡은 대상도 합집합. 한쪽이라도 잡았으면 잡은 것이다.
    # 잃어버리면 그 대상을 새로 추가한 것으로 오해해 다시 기준선을 잡는데,
    # 그 사이에 열린 회차를 놓치게 된다.
    # 반대로 합집합만 하면 삭제한 대상의 기록이 영원히 안 지워진다. 그러면
    # 나중에 그 대상을 다시 등록했을 때 '이미 기준선을 잡았다'고 오해해
    # 기준선을 건너뛰고, 지금 열려 있는 회차를 몽땅 알리게 된다.
    baselined = set(ours.get("baselined", [])) | set(theirs.get("baselined", []))
    if target_ids is not None:
        baselined &= target_ids
    out["baselined"] = sorted(baselined)

    # 게이트 스냅샷은 지점별로 최신 쪽을 쓴다. 값이 다르면 다음 실행에서
    # 변화로 판단해 전체 스캔이 한 번 더 도는 것뿐이라 안전하다.
    gates = dict(theirs.get("gates", {}))
    gates.update(ours.get("gates", {}))
    out["gates"] = gates

    # 단조 증가해야 하는 값들
    for key in ("run_no", "tg_offset", "last_block_warn"):
        vals = [v for v in (ours.get(key), theirs.get(key)) if v is not None]
        if vals:
            out[key] = max(vals)

    out["initialized"] = bool(ours.get("initialized") or theirs.get("initialized"))

    last = [v for v in (ours.get("last_run"), theirs.get("last_run")) if v]
    if last:
        out["last_run"] = max(last)

    return out


def load(path):
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return {}


def main():
    ours_path, theirs_path = sys.argv[1], sys.argv[2]
    config_path = sys.argv[3] if len(sys.argv) > 3 else "config.json"
    ours, theirs = load(ours_path), load(theirs_path)
    merged = merge(ours, theirs, valid_target_ids(config_path))

    with open(theirs_path, "w", encoding="utf-8") as fp:
        json.dump(merged, fp, ensure_ascii=False, indent=2, sort_keys=True)
        fp.write("\n")

    print("state 병합: 우리 seen {} + 원격 seen {} -> {}".format(
        len(ours.get("seen", [])), len(theirs.get("seen", [])),
        len(merged["seen"])))


if __name__ == "__main__":
    main()
