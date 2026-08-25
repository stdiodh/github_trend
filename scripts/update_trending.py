#!/usr/bin/env python3

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
START_MARKER = "<!-- GITHUB-TRENDING:START -->"
END_MARKER = "<!-- GITHUB-TRENDING:END -->"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HISTORY_PATH = REPOSITORY_ROOT / "data" / "trending-history.json"
README_PATH = REPOSITORY_ROOT / "README.md"


def github_get(path, token, parameters=None, allow_not_found=False):
    url = f"{API_ROOT}{path}"
    if parameters:
        url = f"{url}?{urlencode(parameters)}"

    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "github-trend-readme-updater",
            "X-GitHub-Api-Version": API_VERSION,
        },
    )

    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        if allow_not_found and error.code == 404:
            return None
        raise RuntimeError(
            f"GitHub API request failed with HTTP {error.code}: {path}"
        ) from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"GitHub API request failed: {path}: {error}") from error


def parse_repository(item):
    try:
        full_name = item["full_name"]
        stars = item["stargazers_count"]
    except (KeyError, TypeError) as error:
        raise RuntimeError("GitHub API returned an invalid repository") from error

    if not isinstance(full_name, str) or full_name.count("/") != 1:
        raise RuntimeError("GitHub API returned an invalid repository name")
    if type(stars) is not int or stars < 0:
        raise RuntimeError(f"GitHub API returned invalid stars for {full_name}")

    language = item.get("language")
    if language is not None and not isinstance(language, str):
        raise RuntimeError(f"GitHub API returned an invalid language for {full_name}")

    return {
        "full_name": full_name,
        "language": language or "-",
        "stars": stars,
    }


def search_repositories(query, token):
    result = github_get(
        "/search/repositories",
        token,
        {
            "q": query,
            "sort": "stars",
            "order": "desc",
            "per_page": 100,
        },
    )
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise RuntimeError("GitHub search returned an invalid response")
    if result.get("incomplete_results") is True:
        raise RuntimeError("GitHub search returned incomplete results")
    return result["items"]


def collect_repositories(token, today, tracked_names):
    queries = (
        f"created:>={(today - timedelta(days=30)).isoformat()} "
        "stars:>=10 archived:false fork:false",
        f"pushed:>={(today - timedelta(days=7)).isoformat()} "
        "stars:>=100 archived:false fork:false",
    )
    repositories = {}

    for query in queries:
        for item in search_repositories(query, token):
            if isinstance(item, dict) and item.get("private") is True:
                continue
            repository = parse_repository(item)
            repositories[repository["full_name"]] = repository

    for full_name in sorted(tracked_names):
        if full_name in repositories:
            continue
        item = github_get(
            f"/repos/{quote(full_name, safe='/')}",
            token,
            allow_not_found=True,
        )
        if item is None:
            continue
        if isinstance(item, dict) and item.get("private") is True:
            continue
        repository = parse_repository(item)
        repositories[repository["full_name"]] = repository

    return repositories


def load_history(path):
    if not path.exists():
        return {}

    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Failed to read history: {error}") from error

    if not isinstance(history, dict):
        raise RuntimeError("History must be a JSON object")

    for day, repositories in history.items():
        try:
            parsed_day = date.fromisoformat(day)
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"Invalid history date: {day}") from error
        if parsed_day.isoformat() != day or not isinstance(repositories, dict):
            raise RuntimeError(f"Invalid history entry: {day}")
        for full_name, stars in repositories.items():
            if not isinstance(full_name, str) or full_name.count("/") != 1:
                raise RuntimeError(f"Invalid repository in history: {full_name}")
            if type(stars) is not int or stars < 0:
                raise RuntimeError(f"Invalid stars in history for {full_name}")

    return history


def calculate_rankings(repositories, history, today):
    previous = history.get((today - timedelta(days=1)).isoformat(), {})
    week_ago = history.get((today - timedelta(days=7)).isoformat(), {})
    rankings = []

    for repository in repositories.values():
        full_name = repository["full_name"]
        stars = repository["stars"]
        ranking = dict(repository)
        ranking["daily_change"] = (
            stars - previous[full_name] if full_name in previous else None
        )
        ranking["weekly_change"] = (
            stars - week_ago[full_name] if full_name in week_ago else None
        )
        rankings.append(ranking)

    def sort_key(repository):
        daily_change = repository["daily_change"]
        weekly_change = repository["weekly_change"]
        return (
            daily_change is None,
            -daily_change if daily_change is not None else 0,
            weekly_change is None,
            -weekly_change if weekly_change is not None else 0,
            -repository["stars"],
            repository["full_name"].casefold(),
        )

    return sorted(rankings, key=sort_key)


def format_change(change):
    return "-" if change is None else f"{change:+,}"


def render_section(rankings, today):
    lines = [
        START_MARKER,
        "",
        "## 🔥 최근 스타 상승 저장소",
        "",
        f"> {today.isoformat()} 09:00 KST 기준 · 자체 수집한 스타 변화량입니다.",
        "",
        "| 순위 | Repository | Language | Stars | 24시간 | 7일 |",
        "|---:|---|---|---:|---:|---:|",
    ]

    for rank, repository in enumerate(rankings[:10], start=1):
        full_name = repository["full_name"]
        lines.append(
            f"| {rank} | [{full_name}](https://github.com/{full_name}) "
            f"| {repository['language']} | {repository['stars']:,} "
            f"| {format_change(repository['daily_change'])} "
            f"| {format_change(repository['weekly_change'])} |"
        )

    lines.extend(("", END_MARKER))
    return "\n".join(lines)


def update_readme(content, section):
    start_count = content.count(START_MARKER)
    end_count = content.count(END_MARKER)

    if start_count == 0 and end_count == 0:
        if not content:
            return f"{section}\n"
        separator = "\n" if content.endswith(("\n", "\r")) else "\n\n"
        return f"{content}{separator}{section}\n"

    if start_count != 1 or end_count != 1:
        raise RuntimeError("README must contain exactly one matching marker pair")

    start = content.index(START_MARKER)
    end = content.index(END_MARKER)
    if end < start:
        raise RuntimeError("README markers are in the wrong order")

    return f"{content[:start]}{section}{content[end + len(END_MARKER):]}"


def write_if_changed(path, content):
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return True


def run(token, today, history_path=HISTORY_PATH, readme_path=README_PATH):
    history = load_history(history_path)
    readme = readme_path.read_bytes().decode("utf-8") if readme_path.exists() else ""

    eligible_days = [day for day in history if day <= today.isoformat()]
    tracked_names = set(history[max(eligible_days)]) if eligible_days else set()
    repositories = collect_repositories(token, today, tracked_names)
    rankings = calculate_rankings(repositories, history, today)

    history[today.isoformat()] = {
        full_name: repository["stars"]
        for full_name, repository in repositories.items()
    }
    cutoff = today - timedelta(days=7)
    history = {
        day: history[day]
        for day in sorted(history)
        if cutoff <= date.fromisoformat(day) <= today
    }

    updated_readme = update_readme(readme, render_section(rankings, today))
    updated_history = json.dumps(
        history, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"

    history_changed = write_if_changed(history_path, updated_history)
    readme_changed = write_if_changed(readme_path, updated_readme)
    print(
        f"Tracked {len(repositories)} repositories; "
        f"history changed: {history_changed}; README changed: {readme_changed}"
    )


def main():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required")
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    run(token, today)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
