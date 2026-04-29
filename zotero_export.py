"""Zotero PDF Export — pick collections/items, copy PDFs to a flat folder.

Reads Zotero's local SQLite DB (copies it + its journal sidecars to a temp dir
first, so it works whether Zotero is open or closed) and exports chosen PDFs
named "Author Year Title.pdf" into a destination folder. Cross-platform
(Windows / macOS / Linux), pure stdlib.

Run:
    python zotero_export.py
    python zotero_export.py --zotero-dir "/custom/path/to/Zotero"
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

CHECK_ON = "☑"   # ☑
CHECK_OFF = "☐"  # ☐
INVALID_FN_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def candidate_zotero_dirs() -> list[Path]:
    """Likely Zotero data-directory locations across Win/macOS/Linux."""
    home = Path.home()
    return [
        home / "Zotero",                                  # modern default (all OS)
        home / "Documents" / "Zotero",                    # common Windows alt
        home / "Library" / "Application Support" / "Zotero",  # macOS legacy
        home / ".zotero" / "zotero",                      # Linux legacy
    ]


def find_zotero_dir() -> Path | None:
    for d in candidate_zotero_dirs():
        if (d / "zotero.sqlite").exists():
            return d
    return None


# ---------- DB layer ----------

def snapshot_db(zotero_dir: Path) -> Path:
    """Copy zotero.sqlite + any -journal/-wal/-shm sidecars to a temp dir.

    File-copying both main and journal lets SQLite roll forward / back on open
    so we get a consistent snapshot even when Zotero is running.
    """
    src = zotero_dir / "zotero.sqlite"
    if not src.exists():
        raise FileNotFoundError(f"zotero.sqlite not found at {src}")
    tmp = Path(tempfile.mkdtemp(prefix="zotero_export_"))
    for f in zotero_dir.iterdir():
        if f.name == "zotero.sqlite" or f.name.startswith("zotero.sqlite-"):
            if f.suffix == ".bak" or f.name.endswith(".1.bak"):
                continue
            shutil.copy2(f, tmp / f.name)
    return tmp / "zotero.sqlite"


def load_libraries(con: sqlite3.Connection) -> list[tuple[int, str]]:
    rows = con.execute("""
        SELECT l.libraryID,
               COALESCE(g.name, CASE WHEN l.type = 'user' THEN 'My Library' ELSE l.type END) AS name
        FROM libraries l
        LEFT JOIN groups g ON g.libraryID = l.libraryID
        ORDER BY l.libraryID
    """).fetchall()
    return rows


def load_collections(con: sqlite3.Connection, library_id: int) -> list[tuple[int, str, int | None]]:
    return con.execute("""
        SELECT collectionID, collectionName, parentCollectionID
        FROM collections
        WHERE libraryID = ?
        ORDER BY collectionName COLLATE NOCASE
    """, (library_id,)).fetchall()


def load_items_in_collection(con: sqlite3.Connection, collection_id: int) -> list[tuple[int, str, str, str]]:
    return con.execute("""
        SELECT
          i.itemID,
          COALESCE((
            SELECT cr.lastName
              FROM itemCreators ic
              JOIN creators cr ON cr.creatorID = ic.creatorID
             WHERE ic.itemID = i.itemID
             ORDER BY ic.orderIndex LIMIT 1
          ), '') AS author,
          COALESCE((
            SELECT idv.value FROM itemData id
              JOIN itemDataValues idv ON idv.valueID = id.valueID
              JOIN fields f ON f.fieldID = id.fieldID
             WHERE id.itemID = i.itemID AND f.fieldName = 'date'
          ), '') AS date,
          COALESCE((
            SELECT idv.value FROM itemData id
              JOIN itemDataValues idv ON idv.valueID = id.valueID
              JOIN fields f ON f.fieldID = id.fieldID
             WHERE id.itemID = i.itemID AND f.fieldName = 'title'
          ), '(no title)') AS title
        FROM items i
        JOIN collectionItems ci ON ci.itemID = i.itemID
        WHERE ci.collectionID = ?
          AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
          AND i.itemID NOT IN (SELECT itemID FROM itemAttachments WHERE parentItemID IS NOT NULL)
        ORDER BY author COLLATE NOCASE, date
    """, (collection_id,)).fetchall()


def find_pdf_attachment(con: sqlite3.Connection, parent_item_id: int) -> tuple[str, str] | None:
    """Return (attachment_key, filename_without_storage_prefix) or None."""
    row = con.execute("""
        SELECT i.key, ia.path
        FROM itemAttachments ia
        JOIN items i ON i.itemID = ia.itemID
        WHERE ia.parentItemID = ?
          AND ia.contentType = 'application/pdf'
          AND ia.path LIKE 'storage:%'
          AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
        ORDER BY ia.itemID
        LIMIT 1
    """, (parent_item_id,)).fetchone()
    if row is None:
        return None
    key, path = row
    return key, path[len("storage:"):]


def get_item_meta(con: sqlite3.Connection, item_id: int) -> tuple[str, str, str]:
    """(author_lastname, year, title) for filename construction."""
    row = con.execute("""
        SELECT
          COALESCE((
            SELECT cr.lastName
              FROM itemCreators ic
              JOIN creators cr ON cr.creatorID = ic.creatorID
             WHERE ic.itemID = i.itemID
             ORDER BY ic.orderIndex LIMIT 1
          ), 'Unknown') AS author,
          COALESCE((
            SELECT idv.value FROM itemData id
              JOIN itemDataValues idv ON idv.valueID = id.valueID
              JOIN fields f ON f.fieldID = id.fieldID
             WHERE id.itemID = i.itemID AND f.fieldName = 'date'
          ), '') AS date,
          COALESCE((
            SELECT idv.value FROM itemData id
              JOIN itemDataValues idv ON idv.valueID = id.valueID
              JOIN fields f ON f.fieldID = id.fieldID
             WHERE id.itemID = i.itemID AND f.fieldName = 'title'
          ), 'Untitled') AS title
        FROM items i WHERE i.itemID = ?
    """, (item_id,)).fetchone()
    return row if row else ("Unknown", "", "Untitled")


# ---------- filename helpers ----------

def extract_year(date_str: str) -> str:
    m = re.search(r"\d{4}", date_str or "")
    return m.group(0) if m else "n.d."


def safe_filename(author: str, year: str, title: str, max_total: int = 180) -> str:
    author = INVALID_FN_CHARS.sub(" ", (author or "Unknown")).strip() or "Unknown"
    year = INVALID_FN_CHARS.sub(" ", (year or "n.d.")).strip() or "n.d."
    title = INVALID_FN_CHARS.sub(" ", (title or "Untitled")).strip() or "Untitled"
    title = re.sub(r"\s+", " ", title)
    # leave room for ".pdf" plus a possible " (10)" suffix
    head = f"{author} {year} "
    title_room = max(20, max_total - len(head) - len(".pdf") - 5)
    if len(title) > title_room:
        title = title[:title_room].rstrip()
    return f"{author} {year} {title}.pdf"


def unique_path(out_dir: Path, name: str) -> Path:
    target = out_dir / name
    if not target.exists():
        return target
    stem, ext = os.path.splitext(name)
    i = 2
    while True:
        candidate = out_dir / f"{stem} ({i}){ext}"
        if not candidate.exists():
            return candidate
        i += 1


# ---------- GUI ----------

class App:
    def __init__(self, root: tk.Tk, zotero_dir: Path):
        self.root = root
        self.zotero_dir = zotero_dir
        self.storage_dir = zotero_dir / "storage"

        snapshot = snapshot_db(zotero_dir)
        self.con = sqlite3.connect(snapshot)
        self.con.row_factory = None  # tuples are fine

        # itemID -> (author, year, title) for everything currently selected
        self.selected: dict[int, tuple[str, str, str]] = {}
        # itemID -> tree row id for the currently displayed collection
        self.row_for_item: dict[int, str] = {}
        self.current_collection_id: int | None = None

        self._build_ui()
        self._populate_collections()

    def _build_ui(self) -> None:
        self.root.title("Zotero PDF Export")
        self.root.geometry("1100x650")

        outer = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 0))

        # --- left: collections tree ---
        left = ttk.Frame(outer)
        outer.add(left, weight=1)
        ttk.Label(left, text="Libraries & collections", font=("", 10, "bold")).pack(anchor="w")
        self.col_tree = ttk.Treeview(left, show="tree", selectmode="browse")
        col_scroll = ttk.Scrollbar(left, orient="vertical", command=self.col_tree.yview)
        self.col_tree.configure(yscrollcommand=col_scroll.set)
        self.col_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        col_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.col_tree.bind("<<TreeviewSelect>>", self._on_collection_select)

        # --- right: items + controls ---
        right = ttk.Frame(outer)
        outer.add(right, weight=3)
        self.items_label = ttk.Label(right, text="Items: (select a collection on the left)", font=("", 10, "bold"))
        self.items_label.pack(anchor="w")

        cols = ("check", "author", "year", "title")
        self.item_tree = ttk.Treeview(right, columns=cols, show="headings", selectmode="none")
        self.item_tree.heading("check", text="")
        self.item_tree.heading("author", text="Author")
        self.item_tree.heading("year", text="Year")
        self.item_tree.heading("title", text="Title")
        self.item_tree.column("check", width=32, anchor="center", stretch=False)
        self.item_tree.column("author", width=160, anchor="w", stretch=False)
        self.item_tree.column("year", width=60, anchor="w", stretch=False)
        self.item_tree.column("title", width=600, anchor="w", stretch=True)

        item_scroll = ttk.Scrollbar(right, orient="vertical", command=self.item_tree.yview)
        self.item_tree.configure(yscrollcommand=item_scroll.set)
        self.item_tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        item_scroll.place(in_=self.item_tree, relx=1.0, rely=0, relheight=1.0, anchor="ne")
        self.item_tree.bind("<Button-1>", self._on_item_click)
        self.item_tree.bind("<space>", self._on_item_space)

        # buttons row
        btn_row = ttk.Frame(right)
        btn_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(btn_row, text="Select all in collection", command=self._select_all_visible).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Unselect all in collection", command=self._unselect_all_visible).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btn_row, text="Clear ALL selections", command=self._clear_all).pack(side=tk.LEFT, padx=(18, 0))
        self.count_var = tk.StringVar(value="Selected for export: 0 items")
        ttk.Label(right, textvariable=self.count_var).pack(anchor="w", pady=(4, 0))

        # --- bottom: output + actions ---
        bottom = ttk.Frame(self.root)
        bottom.pack(fill=tk.X, padx=8, pady=8)
        ttk.Label(bottom, text="Output folder:").grid(row=0, column=0, sticky="w")
        # Default output: Desktop if it exists (typical on Win/macOS), else home dir.
        desktop = Path.home() / "Desktop"
        default_out = (desktop if desktop.is_dir() else Path.home()) / "ZoteroExport"
        self.out_var = tk.StringVar(value=str(default_out))
        ttk.Entry(bottom, textvariable=self.out_var).grid(row=0, column=1, sticky="ew", padx=(6, 6))
        ttk.Button(bottom, text="Browse…", command=self._browse_out).grid(row=0, column=2)
        ttk.Button(bottom, text="Export PDFs", command=self._export).grid(row=0, column=3, padx=(12, 0))
        ttk.Button(bottom, text="Quit", command=self.root.destroy).grid(row=0, column=4, padx=(6, 0))
        bottom.columnconfigure(1, weight=1)

    # --- collections ---

    def _populate_collections(self) -> None:
        for lib_id, lib_name in load_libraries(self.con):
            lib_node = self.col_tree.insert("", "end", iid=f"L{lib_id}", text=f"📚 {lib_name}", open=True)
            cols = load_collections(self.con, lib_id)
            # build parent->children index
            children: dict[int | None, list[tuple[int, str, int | None]]] = {}
            for cid, name, parent in cols:
                children.setdefault(parent, []).append((cid, name, parent))

            def insert_children(parent_node: str, parent_id: int | None) -> None:
                for cid, name, _ in children.get(parent_id, []):
                    node = self.col_tree.insert(parent_node, "end", iid=f"C{cid}", text=f"📁 {name}")
                    insert_children(node, cid)

            insert_children(lib_node, None)

    def _on_collection_select(self, _event=None) -> None:
        sel = self.col_tree.selection()
        if not sel:
            return
        node = sel[0]
        if not node.startswith("C"):
            return
        cid = int(node[1:])
        self.current_collection_id = cid
        name = self.col_tree.item(node, "text").lstrip("📁 ").strip()
        self.items_label.configure(text=f"Items in: {name}")
        self._load_items(cid)

    def _load_items(self, collection_id: int) -> None:
        self.item_tree.delete(*self.item_tree.get_children())
        self.row_for_item.clear()
        for item_id, author, date, title in load_items_in_collection(self.con, collection_id):
            year = extract_year(date)
            check = CHECK_ON if item_id in self.selected else CHECK_OFF
            row_id = self.item_tree.insert("", "end", values=(check, author or "(no author)", year, title))
            self.row_for_item[item_id] = row_id
        self._update_count()

    # --- item checking ---

    def _toggle_row(self, row_id: str) -> None:
        # find the item_id for this row
        item_id = next((iid for iid, rid in self.row_for_item.items() if rid == row_id), None)
        if item_id is None:
            return
        current = self.item_tree.set(row_id, "check")
        if current == CHECK_ON:
            self.selected.pop(item_id, None)
            self.item_tree.set(row_id, "check", CHECK_OFF)
        else:
            author, date, title = get_item_meta(self.con, item_id)
            self.selected[item_id] = (author, extract_year(date), title)
            self.item_tree.set(row_id, "check", CHECK_ON)
        self._update_count()

    def _on_item_click(self, event) -> None:
        region = self.item_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.item_tree.identify_column(event.x)  # like '#1'
        row_id = self.item_tree.identify_row(event.y)
        if not row_id:
            return
        # toggle if user clicks the check column OR anywhere on the row (more discoverable)
        if col == "#1":
            self._toggle_row(row_id)
        else:
            # also toggle on row click — more forgiving than requiring the tiny check column
            self._toggle_row(row_id)

    def _on_item_space(self, _event) -> None:
        # space toggles every "focused" row (Treeview supports focus even with selectmode=none)
        focus = self.item_tree.focus()
        if focus:
            self._toggle_row(focus)

    def _select_all_visible(self) -> None:
        for item_id, row_id in self.row_for_item.items():
            if item_id not in self.selected:
                author, date, title = get_item_meta(self.con, item_id)
                self.selected[item_id] = (author, extract_year(date), title)
                self.item_tree.set(row_id, "check", CHECK_ON)
        self._update_count()

    def _unselect_all_visible(self) -> None:
        for item_id, row_id in self.row_for_item.items():
            if item_id in self.selected:
                self.selected.pop(item_id, None)
                self.item_tree.set(row_id, "check", CHECK_OFF)
        self._update_count()

    def _clear_all(self) -> None:
        if not self.selected:
            return
        if not messagebox.askyesno("Clear all", f"Clear all {len(self.selected)} selections?"):
            return
        self.selected.clear()
        for row_id in self.row_for_item.values():
            self.item_tree.set(row_id, "check", CHECK_OFF)
        self._update_count()

    def _update_count(self) -> None:
        self.count_var.set(f"Selected for export: {len(self.selected)} items")

    # --- output / export ---

    def _browse_out(self) -> None:
        d = filedialog.askdirectory(initialdir=self.out_var.get() or str(Path.home()))
        if d:
            self.out_var.set(d)

    def _export(self) -> None:
        if not self.selected:
            messagebox.showinfo("Nothing selected", "Pick at least one item first.")
            return
        out_dir = Path(self.out_var.get()).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Output folder", f"Could not create folder:\n{e}")
            return

        copied: list[str] = []
        missing: list[str] = []
        errors: list[str] = []

        for item_id, (author, year, title) in self.selected.items():
            attach = find_pdf_attachment(self.con, item_id)
            if attach is None:
                missing.append(f"{author} {year} {title[:60]}")
                continue
            key, original_name = attach
            src = self.storage_dir / key / original_name
            if not src.exists():
                missing.append(f"{author} {year} {title[:60]}  (file missing on disk)")
                continue
            dst = unique_path(out_dir, safe_filename(author, year, title))
            try:
                shutil.copy2(src, dst)
                copied.append(dst.name)
            except OSError as e:
                errors.append(f"{dst.name}: {e}")

        msg = [f"Exported {len(copied)} PDF(s) to:\n{out_dir}"]
        if missing:
            msg.append(f"\nSkipped {len(missing)} item(s) without a usable PDF:")
            msg.extend(f"  • {m}" for m in missing[:15])
            if len(missing) > 15:
                msg.append(f"  …and {len(missing) - 15} more")
        if errors:
            msg.append(f"\n{len(errors)} copy error(s):")
            msg.extend(f"  • {e}" for e in errors[:10])
        messagebox.showinfo("Export complete", "\n".join(msg))


# ---------- main ----------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export PDFs from your local Zotero library.")
    p.add_argument("--zotero-dir", type=Path, default=None,
                   help="Path to your Zotero data directory. If omitted, common locations are auto-detected.")
    return p.parse_args()


def resolve_zotero_dir(cli_dir: Path | None) -> Path | None:
    """Pick the Zotero dir from CLI flag, auto-detect, or a popup folder picker."""
    if cli_dir is not None:
        if (cli_dir / "zotero.sqlite").exists():
            return cli_dir
        messagebox.showerror(
            "Zotero not found",
            f"No zotero.sqlite in:\n{cli_dir}\n\n"
            "Open Zotero → Settings → Advanced → Files and Folders to see your data directory."
        )
        return None

    auto = find_zotero_dir()
    if auto is not None:
        return auto

    messagebox.showinfo(
        "Locate Zotero",
        "Couldn't find your Zotero data folder automatically.\n\n"
        "On the next screen, pick the folder that contains 'zotero.sqlite' "
        "(in Zotero: Settings → Advanced → Files and Folders)."
    )
    chosen = filedialog.askdirectory(title="Select your Zotero data folder")
    if not chosen:
        return None
    chosen_path = Path(chosen)
    if not (chosen_path / "zotero.sqlite").exists():
        messagebox.showerror("Zotero not found", f"No zotero.sqlite in:\n{chosen_path}")
        return None
    return chosen_path


def main() -> int:
    args = parse_args()

    # Crisper text on high-DPI Windows
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    root.withdraw()  # hide root until we know we have a Zotero dir

    zotero_dir = resolve_zotero_dir(args.zotero_dir)
    if zotero_dir is None:
        root.destroy()
        return 2

    root.deiconify()
    try:
        App(root, zotero_dir)
    except Exception as e:
        messagebox.showerror("Startup error", str(e))
        raise
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
