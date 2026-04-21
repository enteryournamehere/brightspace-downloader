"""Download Brightspace folders and files marked in the configuration.

Walks every marked folder recursively and also downloads each individually
marked file. File topics are written as-is, link topics as a `.url` shortcut.
Re-runs are cheap: files are skipped when the local size matches
Content-Length.

Output tree:  <root>/<course-name>/<folder-path>/<filename>
"""
import argparse
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

import auth
import config
from graphql import MODULE_QUERY, ROOT_QUERY, fetch_courses, gql


INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name):
    name = INVALID.sub("_", name).strip().rstrip(".")
    return name or "_"


def _parse_topic_url(topic_id):
    """Topic ids look like https://<tenant>/<ouId>/content/topics/<topicId>."""
    parts = urlparse(topic_id).path.strip("/").split("/")
    # [ouId, "content", "topics", topicId]
    return parts[0], parts[-1]


def walk(token, module, path, under_marked, modules, topics):
    under = under_marked or module["id"] in modules
    here = path + [sanitize(module["title"])]
    children = module.get("children")
    if children is None:
        children = gql(token, MODULE_QUERY, {"id": module["id"]})["contentModule"]["children"]
    for c in children:
        if c["__typename"] == "ContentModule":
            yield from walk(token, c, here, under, modules, topics)
        elif under or c["id"] in topics:
            yield here, c


def walk_course(token, course_id, modules, topics):
    roots = gql(token, ROOT_QUERY, {"id": course_id})["contentRoot"]["modules"]
    for m in roots:
        yield from walk(token, m, [], False, set(modules), set(topics))


def download_topic(token, domain, topic, dest_dir):
    ou_id, topic_id = _parse_topic_url(topic["id"])
    base = f"https://{domain}/d2l/api/le/1.40/{ou_id}/content/topics/{topic_id}"

    meta_r = requests.get(base, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    if meta_r.status_code != 200:
        return "err", f"metadata {meta_r.status_code}"
    meta = meta_r.json()
    ttype = meta.get("TypeIdentifier")
    title = sanitize(meta.get("Title") or topic["title"] or "topic")
    dest_dir.mkdir(parents=True, exist_ok=True)

    if ttype == "Link":
        path = dest_dir / f"{title}.url"
        path.write_text(f"[InternetShortcut]\nURL={meta.get('Url', '')}\n")
        return "link", path.name

    with requests.get(f"{base}/file",
                      headers={"Authorization": f"Bearer {token}"},
                      timeout=60, stream=True) as r:
        if r.status_code != 200:
            return "err", f"file {r.status_code}"
        cd = r.headers.get("Content-Disposition", "")
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd)
        filename = sanitize(unquote(m.group(1))) if m else f"{title}.bin"
        path = dest_dir / filename

        size = r.headers.get("Content-Length")
        if path.exists() and size and path.stat().st_size == int(size):
            return "skip", filename

        tmp = path.with_suffix(path.suffix + ".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(64 * 1024):
                f.write(chunk)
        tmp.rename(path)
        return "get", filename


MARKERS = {"get": "+", "skip": "=", "link": "~", "err": "!"}


def run(token, cfg, root):
    """Download every marked target. Returns counts dict."""
    domain = cfg["domain"]
    targets = config.all_targets(cfg)
    if not targets:
        print("Nothing marked for download. Run configure.py to pick folders/files.")
        return {"get": 0, "skip": 0, "link": 0, "err": 0}

    enrollments = {c["id"]: c["name"] for c in fetch_courses(token)}
    counts = {"get": 0, "skip": 0, "link": 0, "err": 0}

    for course_id, raw in targets.items():
        course_name = enrollments.get(course_id)
        if not course_name:
            print(f"! Skipping unknown course {course_id}")
            continue
        course_dir = root / sanitize(course_name)
        print(f"\n=== {course_name} ===")

        t = config.normalize_targets(raw)
        for path_parts, topic in walk_course(token, course_id, t["modules"], t["topics"]):
            dest = course_dir.joinpath(*path_parts)
            status, name = download_topic(token, domain, topic, dest)
            counts[status] += 1
            rel = dest.relative_to(course_dir) / name
            print(f"  {MARKERS[status]} {rel}")

    print(f"\n{counts['get']} downloaded, {counts['skip']} up-to-date, "
          f"{counts['link']} links, {counts['err']} errors")
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./brightspace_sync",
                    help="Download root (default: ./brightspace_sync)")
    args = ap.parse_args()

    cfg = config.load()
    if not cfg.get("domain"):
        sys.exit("No domain cached. Run configure.py first.")
    token = auth.ensure_token(cfg)
    run(token, cfg, Path(args.out))


if __name__ == "__main__":
    main()
