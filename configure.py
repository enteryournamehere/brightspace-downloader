"""Main menu + interactive TUI for marking Brightspace folders and files.

Launches a simple text menu showing the current configuration. From there:
  [e] open the Textual TUI to pick courses/folders/files
  [d] download everything that has been marked
  [q] quit

The TUI caches the Brightspace domain and the set of marked targets in
~/.config/brightspace_downloader/config.json. A single OAuth login is done on
first run (or when the cached token has expired); the access token is cached
separately under ~/.cache/brightspace_downloader/.
"""
import sys
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Tree as _Tree

import auth
import config
import download
from graphql import CONTENT_ITEM_FRAGMENT, MODULE_QUERY, ROOT_QUERY, fetch_courses, gql


class Tree(_Tree):
    BINDINGS = [
        Binding("left", "my_left", show=False),
        Binding("right", "my_right", show=False),
    ]

    def action_my_right(self):
        node = self.cursor_node
        if node is not None and node.allow_expand and not node.is_expanded:
            node.expand()

    def action_my_left(self):
        node = self.cursor_node
        if node is None:
            return
        if node.allow_expand and node.is_expanded:
            node.collapse()
            return
        parent = node.parent
        if parent is not None and parent is not self.root.parent:
            self.select_node(parent)
            self.scroll_to_node(parent)


class CourseList(Screen):
    BINDINGS = [
        Binding("escape", "app.quit", "Back to menu"),
        Binding("up", "cursor_up", show=False),
        Binding("down", "cursor_down", show=False),
    ]

    def __init__(self, courses):
        super().__init__()
        self.courses = courses
        self.filtered = list(courses)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="type to filter...", id="filter")
        yield ListView(id="courses")
        yield Footer()

    def on_mount(self):
        self.refresh_list()
        self.query_one("#filter", Input).focus()

    def refresh_list(self):
        lv = self.query_one("#courses", ListView)
        lv.clear()
        for c in self.filtered:
            pin = "★ " if c["pinned"] else "  "
            dim = "" if c["active"] else " [dim](inactive)[/dim]"
            lv.append(ListItem(Label(f"{pin}{c['num']:>7}  {c['name']}{dim}", markup=True)))
        if self.filtered:
            lv.index = 0

    def on_input_changed(self, event: Input.Changed):
        q = event.value.lower()
        self.filtered = [c for c in self.courses if q in c["name"].lower() or q in c["num"]]
        self.refresh_list()

    def on_input_submitted(self, _event: Input.Submitted):
        self._open_selected()

    def _open_selected(self):
        idx = self.query_one("#courses", ListView).index
        if idx is not None and 0 <= idx < len(self.filtered):
            self.app.push_screen(CourseTree(self.filtered[idx]))

    def action_cursor_up(self):
        self.query_one("#courses", ListView).action_cursor_up()

    def action_cursor_down(self):
        self.query_one("#courses", ListView).action_cursor_down()


class CourseTree(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("space", "toggle_download", "Toggle download", priority=True),
    ]

    def __init__(self, course):
        super().__init__()
        self.course = course

    def compose(self):
        yield Header()
        self._tree = Tree(f"{self.course['name']}", id="tree")
        self._tree.show_root = True
        self._tree.root.expand()
        yield self._tree
        yield Footer()

    def _targets(self):
        t = self.app.config.setdefault("download_targets", {})
        entry = t.setdefault(self.course["id"], {"modules": [], "topics": []})
        if isinstance(entry, list):
            entry = {"modules": entry, "topics": []}
            t[self.course["id"]] = entry
        return entry

    @property
    def download_set(self):
        e = self._targets()
        return set(e["modules"]) | set(e["topics"])

    def persist_download(self, kind, id_, add):
        e = self._targets()
        key = "modules" if kind == "module" else "topics"
        ids = set(e[key])
        ids.add(id_) if add else ids.discard(id_)
        e[key] = sorted(ids)
        config.save(self.app.config)

    def on_mount(self):
        self._tree.focus()
        self.load_root()

    def _mark(self, id_):
        return "[green]■[/green] " if id_ in self.download_set else "  "

    def _label_module(self, m):
        return f"{self._mark(m['id'])}📁 {m['title']}"

    def _label_topic(self, t):
        return f"{self._mark(t['id'])}📄 {t['title']}"

    def _add_child(self, parent, c):
        if c["__typename"] == "ContentModule":
            node = parent.add(self._label_module(c),
                              data={"type": "module", "id": c["id"],
                                    "title": c["title"], "loaded": False})
            node.add_leaf("loading...", data={"placeholder": True})
        else:
            parent.add_leaf(self._label_topic(c),
                            data={"type": "topic", "id": c["id"],
                                  "title": c["title"]})

    @work(thread=True, exclusive=True)
    def load_root(self):
        data = gql(self.app.token, ROOT_QUERY, {"id": self.course["id"]})
        self.app.call_from_thread(self._populate_root, data["contentRoot"]["modules"])

    def _populate_root(self, modules):
        for m in modules:
            node = self._tree.root.add(
                self._label_module(m),
                data={"type": "module", "id": m["id"],
                      "title": m["title"], "loaded": True})
            for c in m["children"]:
                self._add_child(node, c)
        self._tree.root.expand()

    def on_tree_node_expanded(self, event):
        node = event.node
        d = node.data or {}
        if d.get("type") == "module" and not d.get("loaded"):
            d["loaded"] = True
            node.remove_children()
            self.load_module(node, d["id"])

    @work(thread=True)
    def load_module(self, node, module_id):
        data = gql(self.app.token, MODULE_QUERY, {"id": module_id})
        self.app.call_from_thread(self._populate_children, node,
                                  data["contentModule"]["children"])

    def _populate_children(self, node, children):
        for c in children:
            self._add_child(node, c)

    def action_toggle_download(self):
        node = self._tree.cursor_node
        if not node or not node.data:
            return
        kind = node.data.get("type")
        if kind not in ("module", "topic"):
            return
        nid = node.data["id"]
        add = nid not in self.download_set
        self.persist_download(kind, nid, add)
        item = {"id": nid, "title": node.data["title"]}
        node.label = self._label_module(item) if kind == "module" else self._label_topic(item)


class TuiApp(App):
    CSS = """
    Input { height: 3; }
    Tree { height: 1fr; }
    """
    TITLE = "Brightspace"

    def __init__(self, token, courses, cfg):
        super().__init__()
        self.token = token
        self.courses = courses
        self.config = cfg

    def on_mount(self):
        self.push_screen(CourseList(self.courses))


def _print_status(cfg):
    domain = cfg.get("domain") or "(not set)"
    courses, modules, topics = config.summary(cfg)
    print()
    print(f"  Domain : {domain}")
    print(f"  Marked : {courses} course(s), {modules} folder(s), {topics} file(s)")
    print(f"  Config : {config.CONFIG_PATH}")
    print()


def _edit(cfg):
    token = auth.ensure_token(cfg)
    print("Loading courses...")
    courses = fetch_courses(token)
    TuiApp(token, courses, cfg).run()


def _download(cfg):
    token = auth.ensure_token(cfg)
    out = input("Download to [default: ./brightspace_sync]: ").strip() or "./brightspace_sync"
    download.run(token, cfg, Path(out))
    input("\nPress enter to return to menu...")


def main():
    cfg = config.load()
    while True:
        print("\n=== Brightspace downloader ===")
        _print_status(cfg)
        choice = input("[e]dit selection  [d]ownload  [q]uit > ").strip().lower()
        if choice == "e":
            _edit(cfg)
            cfg = config.load()
        elif choice == "d":
            try:
                _download(cfg)
            except KeyboardInterrupt:
                print("\nAborted.")
        elif choice in ("q", ""):
            return
        else:
            print(f"Unknown option: {choice!r}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
