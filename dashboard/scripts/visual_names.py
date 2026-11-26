"""Action names, the self-update confirmation and the crash-restart label (card #1896, items 2-12 and 3-5)."""

from __future__ import annotations

from visual_checks import Env, row

APPROVE_CALL = ("POST", "/api/self-update/approve")
CONFIRM_SUB = "Haniel restarts and this node's services pause briefly. Overlap and adopt services keep running."


def _names(env: Env, width: int, height: int) -> None:
    r = env.report
    ctx, page, mock = env.open(width, height)
    approve = page.get_by_role("button", name="Approve", exact=True)
    deploy = page.get_by_role("button", name="Deploy eb_lore", exact=True)
    r.check("g", f"{width}: 자기 갱신 단추는 Approve, 저장소 단추는 Deploy", approve.count() == 1 and deploy.inner_text().strip() == "Deploy")
    r.check("g", f"{width}: 옛 이름(Approve update, Pull)이 화면에 없다", not page.get_by_text("Approve update").count() and not page.get_by_role("button", name="Pull", exact=True).count())
    deploy_group = row(page, "soulstream").get_by_role("button", name="Deploy soulstream", exact=True)
    r.check("g", f"{width}: 서비스가 딸린 저장소 줄의 단추도 Deploy", deploy_group.count() == 1)

    busy = page.locator(".st-busy", has_text="Deploying…")
    r.check("g", f"{width}: 진행 중인 저장소 줄의 표시는 Deploying…(Pulling…이 아니다)", busy.count() == 1 and not page.get_by_text("Pulling").count(), str(busy.count()))

    approve.click()
    dialog = page.locator(".dialog.is-confirm")
    dialog.wait_for(state="visible")
    shown = dialog.inner_text()
    r.check("g", f"{width}: 확인창 제목과 안내 문구", "Update Haniel now?" in shown and CONFIRM_SUB in shown, shown[:160])
    r.check("g", f"{width}: 확인창 단추는 Cancel과 Approve", dialog.get_by_role("button", name="Cancel", exact=True).count() == 1 and dialog.get_by_role("button", name="Approve", exact=True).count() == 1)
    r.check("g", f"{width}: 확인창이 뜨기만 해서는 승인 API를 부르지 않는다", APPROVE_CALL not in mock.calls)
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    page.wait_for_timeout(250)
    r.check("g", f"{width}: Cancel하면 승인 API를 부르지 않고 업데이트 덮개도 없다", APPROVE_CALL not in mock.calls and page.locator(".status-panel").count() == 0)
    approve.click()
    page.locator(".dialog.is-confirm").get_by_role("button", name="Approve", exact=True).click()
    page.wait_for_selector(".status-panel")
    r.check("g", f"{width}: 확인창에서 Approve하면 승인 API를 한 번 부른다", mock.calls.count(APPROVE_CALL) == 1, str(mock.calls.count(APPROVE_CALL)))
    ctx.close()


def _crash_label(env: Env, width: int, height: int) -> None:
    r = env.report
    ctx, page, _ = env.open(width, height)
    if width < 800:
        page.get_by_label("Open menu").click()
    label = page.locator(".st-wait:visible", has_text="Crash restarts queued").first
    label.wait_for(state="visible")
    r.check("g", f"{width}: 크래시 재시작 예약은 Crash restarts queued로 보인다", label.is_visible() and not page.get_by_text("Restart pending").count())
    ctx.close()


def run(env: Env) -> None:
    for width, height in ((1440, 900), (390, 844)):
        _names(env, width, height)
        _crash_label(env, width, height)
