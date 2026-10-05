"""分點抓取的逾時預算：job timeout 永遠不該先觸發。

2026-10-01 與 10-05 兩次 fetch-branch 都在 120 分鐘的 job timeout 被 cancel，
而 cancel 會把後面的「儲存 SQLite 快取」與「觸發重新匯出」標成 skipped——
那一輪抓到的分點資料一筆都沒入庫（10-05 那次是 90.7 分鐘的 Top 10 全丟）。

兩個抓取步驟本來就有 continue-on-error，但那只管「程式崩潰」；job 層級的
timeout 是整個 job 被 cancel，continue-on-error 擋不住。所以改成每步各自
設上限，讓 job timeout 永遠不會先到：步驟逾時 → 該步截斷 → 流程照常往下走
→ 已抓到的部分入庫並發布。

這裡用 YAML 結構解析而不是比對原始碼字串：PR #18 與 #20 的 review 都指出
抓字串的測試換個排版就誤報，而且驗的是寫法不是行為。
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = Path(".github/workflows/fetch-branch.yml")


# 前置（checkout/setup/pip/restore cache）加後置（存快取、dispatch）實測約 1 分鐘，
# 留 10 分鐘餘裕：步驟逾時合計必須比 job 上限至少小這麼多。
MARGIN_MINUTES = 10


@pytest.fixture(scope="module")
def job():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = data["jobs"]
    assert len(jobs) == 1, f"預期單一 job，實得 {list(jobs)}"
    return next(iter(jobs.values()))


def _fetch_steps(job):
    """會連外抓資料、可能跑很久的步驟。"""
    return [s for s in job["steps"] if "run" in s and "stock_chip.branch" in s["run"]]


def _index_of(job, predicate, label):
    """用結構特徵定位步驟，不用名稱比對。

    名稱會互相包含——「儲存 SQLite 快取（必須早於觸發重新匯出）」同時含有
    兩個關鍵字，用子字串配對會抓錯步驟（這個測試第一版就踩到了）。
    """
    hits = [i for i, step in enumerate(job["steps"]) if predicate(step)]
    assert len(hits) == 1, f"預期剛好一個{label}步驟，實得 {len(hits)} 個"
    return hits[0]


def _save_index(job):
    return _index_of(job, lambda s: "cache/save" in (s.get("uses") or ""), "存快取")


def _dispatch_index(job):
    return _index_of(
        job, lambda s: "dispatches" in (s.get("run") or ""), "觸發重新匯出"
    )


def test_every_fetch_step_has_its_own_timeout(job):
    steps = _fetch_steps(job)
    assert steps, "找不到任何分點抓取步驟——這個測試的前提已經不成立"
    missing = [s["name"] for s in steps if not s.get("timeout-minutes")]
    assert not missing, (
        f"這些抓取步驟沒有自己的 timeout-minutes：{missing}。"
        "少了它，單一步驟變慢就會撞到 job 層級的 timeout，"
        "而 job 被 cancel 時存快取與觸發匯出都會被 skipped，整輪白抓"
    )


def test_step_timeouts_stay_under_the_job_timeout(job):
    job_limit = job.get("timeout-minutes")
    assert job_limit, "job 必須有 timeout-minutes，否則預設 360 分會綁住 runner"
    total = sum(s.get("timeout-minutes") or 0 for s in job["steps"])
    assert total + MARGIN_MINUTES <= job_limit, (
        f"步驟逾時合計 {total} 分 + 餘裕 {MARGIN_MINUTES} 分超過 job 上限 "
        f"{job_limit} 分。這樣 job timeout 仍可能先觸發，等於這個修正沒生效"
    )


def test_fetch_steps_tolerate_their_own_timeout(job):
    """截斷不能讓流程中止——兩個步驟都可續傳，截斷只影響收斂速度。"""
    not_tolerated = [
        s["name"] for s in _fetch_steps(job) if not s.get("continue-on-error")
    ]
    assert not not_tolerated, (
        f"這些抓取步驟沒有 continue-on-error：{not_tolerated}。"
        "逾時會讓整個 job 失敗，後面的存快取與觸發匯出就不會跑"
    )


@pytest.mark.parametrize("locate", [_save_index, _dispatch_index],
                         ids=["存快取", "觸發重新匯出"])
def test_publish_steps_run_even_when_cancelled(job, locate):
    """就算 job 被 cancel 也必須跑——否則前面抓到的分點全白費。"""
    step = job["steps"][locate(job)]
    assert step.get("if") == "always()", (
        f"「{step.get('name')}」必須是 if: always()。"
        "runner 失聯或人工取消時若被 skipped，前面抓到的分點就白抓了"
    )


def test_cache_is_saved_before_the_re_export_is_triggered(job):
    """順序錯了會讓被觸發的匯出讀到還沒寫入分點的舊快取。"""
    assert _save_index(job) < _dispatch_index(job), "存快取必須早於觸發重新匯出"
