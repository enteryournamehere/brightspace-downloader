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

from prompt_toolkit import prompt
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Tree as _Tree

from lib import auth, config
import download
from lib.graphql import MODULE_QUERY, ROOT_QUERY, fetch_courses, gql

MARK_FULL    = "[#22c55e]■[/#22c55e] "
MARK_PARTIAL = "[#f97316]■[/#f97316] "
MARK_NONE    = "  "


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
        targets = self.app.config.get("download_targets", {})
        for c in self.filtered:
            pin = "★ " if c["pinned"] else "  "
            dim = "" if c["active"] else " [dim](inactive)[/dim]"
            t = targets.get(c["id"], {})
            if t.get("course"):
                indicator = MARK_FULL
            elif t.get("modules") or t.get("topics"):
                indicator = MARK_PARTIAL
            else:
                indicator = MARK_NONE
            lv.append(ListItem(Label(f"{indicator}{pin}{c['num']:>7}  {c['name']}{dim}", markup=True)))
        if self.filtered:
            lv.index = 0

    def on_screen_resume(self):
        self.refresh_list()

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
        self._tree.root.data = {
            "type": "course",
            "id": self.course["id"],
            "title": self.course["name"],
        }
        self._tree.root.label = self._label_course(self.course)
        self._tree.root.expand()
        yield self._tree
        yield Footer()

    def _targets(self):
        t = self.app.config.setdefault("download_targets", {})
        entry = t.setdefault(self.course["id"], {})
        entry.setdefault("course", False)
        entry.setdefault("modules", [])
        entry.setdefault("topics", [])
        entry.setdefault("module_children", {})
        return entry

    @property
    def course_marked(self):
        return bool(self._targets().get("course"))

    @property
    def download_set(self):
        e = self._targets()
        return set(e["modules"]) | set(e["topics"])

    def persist_download(self, kind, id_, add):
        e = self._targets()
        if kind == "course":
            e["course"] = add
            config.save(self.app.config)
            return
        key = "modules" if kind == "module" else "topics"
        ids = set(e[key])
        ids.add(id_) if add else ids.discard(id_)
        e[key] = sorted(ids)
        config.save(self.app.config)

    def on_mount(self):
        self._tree.focus()
        self.load_root()

    def _record_children(self, parent_id, children):
        mc = self._targets()["module_children"]
        mc[parent_id] = [c["id"] for c in children]
        config.save(self.app.config)

    def _has_marked_descendant(self, module_id):
        children = self._targets()["module_children"].get(module_id, [])
        for cid in children:
            if cid in self.download_set:
                return True
            if self._has_marked_descendant(cid):
                return True
        return False

    def _mark_course(self):
        if self.course_marked:
            return MARK_FULL
        t = self._targets()
        if t["modules"] or t["topics"]:
            return MARK_PARTIAL
        return MARK_NONE

    def _mark_module(self, id_):
        if self.course_marked or id_ in self.download_set:
            return MARK_FULL
        if self._has_marked_descendant(id_):
            return MARK_PARTIAL
        return MARK_NONE

    def _mark_topic(self, id_):
        if self.course_marked or id_ in self.download_set:
            return MARK_FULL
        return MARK_NONE

    def _label_course(self, c):
        return f"{self._mark_course()}📚 {c['name']}"

    def _label_module(self, m):
        return f"{self._mark_module(m['id'])}📁 {m['title']}"

    def _label_topic(self, t):
        return f"{self._mark_topic(t['id'])}📄 {t['title']}"

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
            self._record_children(m["id"], m["children"])
            node = self._tree.root.add(
                self._label_module(m),
                data={"type": "module", "id": m["id"],
                      "title": m["title"], "loaded": True})
            for c in m["children"]:
                self._add_child(node, c)
        self._tree.root.expand()
        self._refresh_marks(self._tree.root)

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
        self._record_children(node.data["id"], children)
        for c in children:
            self._add_child(node, c)
        self._refresh_marks(self._tree.root)

    def _refresh_marks(self, node):
        d = node.data or {}
        kind = d.get("type")
        if kind == "course":
            node.label = self._label_course({"name": d["title"]})
        elif kind == "module":
            node.label = self._label_module({"id": d["id"], "title": d["title"]})
        elif kind == "topic":
            node.label = self._label_topic({"id": d["id"], "title": d["title"]})
        for child in node.children:
            self._refresh_marks(child)

    def action_toggle_download(self):
        node = self._tree.cursor_node
        if not node or not node.data:
            return
        kind = node.data.get("type")
        if kind not in ("course", "module", "topic"):
            return
        if kind == "course":
            add = not self.course_marked
            self.persist_download("course", self.course["id"], add)
            self._refresh_marks(self._tree.root)
            return

        nid = node.data["id"]
        add = nid not in self.download_set
        self.persist_download(kind, nid, add)
        self._refresh_marks(self._tree.root)


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


def _menu_choice() -> str:
    kb = KeyBindings()

    @kb.add("e")
    @kb.add("E")
    def _choose_edit(event):
        event.app.exit(result="e")

    @kb.add("d")
    @kb.add("D")
    def _choose_download(event):
        event.app.exit(result="d")

    @kb.add("q")
    @kb.add("Q")
    def _choose_quit(event):
        event.app.exit(result="q")

    @kb.add("enter")
    def _choose_enter(event):
        event.app.exit(result="q")

    return prompt(
        HTML(
            "<ansigreen>[e]</ansigreen>dit selection  "
            "<ansiyellow>[d]</ansiyellow>ownload  "
            "<ansired>[q]</ansired>uit > "
        ),
        key_bindings=kb,
        mouse_support=False,
    ).strip().lower()


def main():
    cfg = config.load()
    while True:
        print("\n=== Brightspace downloader ===")
        _print_status(cfg)
        choice = _menu_choice()
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
