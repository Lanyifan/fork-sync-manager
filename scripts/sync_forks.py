"""Sync personal GitHub forks using backed-up, non-forced reference updates."""

import html
import json
import os
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


LABELS = {
    "up_to_date": "已是最新",
    "would_sync": "可以同步（只检查）",
    "synced": "同步成功",
    "local_ahead": "fork 有额外提交，跳过",
    "diverged": "双方历史分叉，跳过",
    "excluded": "配置排除",
    "archived": "归档仓库，跳过",
    "changed_during_check": "检查期间分支变化，跳过",
    "error": "失败",
}


class GitHubAPI:
    def __init__(self, token):
        if not token:
            raise ValueError("缺少 FORK_SYNC_TOKEN Secret。请按 README 配置。")
        self.token = token

    def request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            "https://api.github.com" + path,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "personal-fork-sync",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                message = json.loads(exc.read()).get("message", exc.reason)
            except (ValueError, AttributeError):
                message = str(exc.reason)
            message = str(message).replace(self.token, "[REDACTED]")
            raise RuntimeError(
                f"GitHub API {exc.code}: {message[:400]}；请求 {method} {path}"
            ) from None
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"GitHub API 网络请求失败：{type(exc).__name__}") from None


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("config.json 顶层必须是 JSON 对象。")
    allowed = {"include_repositories", "exclude_repositories", "branch_overrides"}
    if set(config) - allowed:
        raise ValueError("config.json 含未知配置字段，请检查拼写。")
    for key in ("include_repositories", "exclude_repositories"):
        values = config.get(key, [])
        if not isinstance(values, list) or any(
            not isinstance(value, str)
            or len(value.split("/")) != 2
            or not all(value.split("/"))
            for value in values
        ):
            raise ValueError(f"{key} 必须是仓库全名数组，例如 [\"alice/demo\"]。")
    overrides = config.get("branch_overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("branch_overrides 必须是 JSON 对象。")
    for repo, branches in overrides.items():
        if len(repo.split("/")) != 2 or not all(repo.split("/")):
            raise ValueError("branch_overrides 的键必须是 owner/repo。")
        if not isinstance(branches, list) or not branches or any(
            not isinstance(branch, str) or not branch.strip() for branch in branches
        ):
            raise ValueError(f"{repo} 的分支配置必须是非空字符串数组。")
    return config


def owned_repositories(api):
    page = 1
    while True:
        query = urlencode({"affiliation": "owner", "per_page": 100,
                           "sort": "full_name", "page": page})
        repos = api.request("GET", "/user/repos?" + query)
        yield from repos
        if len(repos) < 100:
            break
        page += 1


def reference_sha(api, repo, branch):
    path = f"/repos/{repo}/git/ref/heads/{quote(branch, safe='')}"
    return api.request("GET", path)["object"]["sha"]


def sync_branch(api, repo, upstream, branch, dry_run, run_label, result):
    # 固定双方 SHA，避免检查和更新之间上游继续推送导致比较对象变化。
    old_sha = reference_sha(api, repo, branch)
    target_sha = reference_sha(api, upstream, branch)
    result.update(old_sha=old_sha, upstream_sha=target_sha)
    if old_sha == target_sha:
        result.update(status="up_to_date", message="分支 SHA 与上游相同。")
        return

    comparison = api.request(
        "GET", f"/repos/{repo}/compare/{old_sha}...{target_sha}?per_page=1"
    )
    ahead, behind = comparison["ahead_by"], comparison["behind_by"]
    result.update(upstream_ahead_by=ahead, fork_ahead_by=behind)
    if behind > 0:
        result.update(
            status="diverged" if ahead > 0 else "local_ahead",
            message=f"上游有 {ahead} 个额外提交，fork 有 {behind} 个额外提交。未修改分支。",
        )
        return
    if comparison["status"] != "ahead" or ahead <= 0:
        raise RuntimeError("无法确认分支可快进更新，已停止该分支的操作。")

    if dry_run:
        result.update(status="would_sync", message=f"上游新增 {ahead} 个提交，可快进更新。")
        return

    if reference_sha(api, repo, branch) != old_sha:
        result.update(status="changed_during_check", message="目标分支已变化，留待下一次检查。")
        return

    tag = f"sync-backup/{branch}/{run_label}-{old_sha[:12]}"
    backup = api.request(
        "POST", f"/repos/{repo}/git/refs",
        {"ref": "refs/tags/" + tag, "sha": old_sha},
    )
    result["backup_tag"] = tag
    if backup["object"]["sha"] != old_sha:
        raise RuntimeError("备份 tag 指向异常，已停止更新。")

    # GitHub 在服务器端检查快进关系；即使有人同时推送，也不强制覆盖。
    update = api.request(
        "PATCH", f"/repos/{repo}/git/refs/heads/{quote(branch, safe='')}",
        {"sha": target_sha, "force": False},
    )
    result["after_sha"] = update["object"]["sha"]
    if result["after_sha"] != target_sha:
        raise RuntimeError("更新响应的 SHA 与目标不一致，请检查分支。备份 tag 已保留。")
    if reference_sha(api, repo, branch) != target_sha:
        raise RuntimeError("更新后分支再次发生变化，请检查分支。备份 tag 已保留。")
    result.update(status="synced", message=f"已快进更新 {ahead} 个提交，并保留同步前的备份 tag。")


def scan_and_sync(api, config, dry_run, run_label, results):
    login = api.request("GET", "/user")["login"].lower()
    include = {value.lower() for value in config.get("include_repositories", [])}
    exclude = {value.lower() for value in config.get("exclude_repositories", [])}
    overrides = {key.lower(): value for key, value in config.get("branch_overrides", {}).items()}
    found = set()
    for entry in owned_repositories(api):
        if entry["owner"]["login"].lower() != login or not entry["fork"]:
            continue
        repo = entry["full_name"]
        found.add(repo.lower())
        if include and repo.lower() not in include:
            continue
        result = {"repository": repo, "branch": "", "status": "error"}
        if repo.lower() in exclude or entry.get("archived") or entry.get("disabled"):
            result.update(
                status="excluded" if repo.lower() in exclude else "archived",
                message="按配置或仓库状态跳过。",
            )
            results.append(result)
            continue
        try:
            detail = api.request("GET", f"/repos/{repo}")
            parent = detail.get("parent")
            if not parent:
                raise RuntimeError("无法获取上游父仓库信息。")
            upstream = parent["full_name"]
            branches = overrides.get(repo.lower(), [parent["default_branch"]])
        except Exception as exc:
            result["message"] = str(exc)
            results.append(result)
            continue
        for branch in dict.fromkeys(branches):
            result = {"repository": repo, "upstream": upstream,
                      "branch": branch, "status": "error"}
            results.append(result)
            try:
                sync_branch(api, repo, upstream, branch, dry_run, run_label, result)
            except Exception as exc:
                result.update(status="error", message=str(exc))
    for missing in sorted(include - found):
        results.append({"repository": missing, "branch": "", "status": "error",
                        "message": "未扫描到此个人 fork，请检查仓库名称、归属和 Token 授权。"})


def write_reports(results, dry_run, fatal_error=None):
    counts = Counter(row["status"] for row in results)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "dry_run": dry_run, "counts": dict(counts),
              "fatal_error": fatal_error, "results": results}
    Path("report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def cell(value):
        return html.escape(str(value)).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    lines = ["# Fork 同步报告", "", f"模式：{'只检查' if dry_run else '正式同步'}。时间：{report['generated_at']}。", ""]
    if fatal_error:
        lines.extend(["全局错误：" + cell(fatal_error), ""])
    if counts:
        lines.extend(["；".join(f"{LABELS[key]}：{value}" for key, value in counts.items()), ""])
    else:
        lines.extend(["未找到符合配置且可见的个人 fork。请检查 Token 授权和仓库归属。", ""])
    lines.extend(["| 仓库 | 分支 | 结果 | 同步前 SHA | 上游 SHA | 备份 tag | 说明 |",
                  "| --- | --- | --- | --- | --- | --- | --- |"])
    for row in results:
        values = [row["repository"], row.get("branch", ""), LABELS[row["status"]],
                  row.get("old_sha", "")[:12], row.get("upstream_sha", "")[:12],
                  row.get("backup_tag", ""), row.get("message", "")]
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
        print(f"[{row['status']}] {row['repository']} {row.get('branch', '')}: {row.get('message', '')}")
    Path("report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    results, fatal_error = [], None
    raw_mode = os.environ.get("DRY_RUN", "true").lower()
    dry_run = raw_mode != "false"
    try:
        if raw_mode not in {"true", "false"}:
            raise ValueError("DRY_RUN 必须为 true 或 false。")
        config = validate_config(json.loads(Path("config.json").read_text(encoding="utf-8")))
        api = GitHubAPI(os.environ.get("GH_TOKEN", ""))
        run_label = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        scan_and_sync(api, config, dry_run, run_label, results)
    except Exception as exc:
        fatal_error = str(exc)
        print("全局错误：" + fatal_error, file=sys.stderr)
    write_reports(results, dry_run, fatal_error)
    return 1 if fatal_error or any(row["status"] == "error" for row in results) else 0


if __name__ == "__main__":
    sys.exit(main())
