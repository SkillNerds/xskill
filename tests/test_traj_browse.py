"""全文轨迹检索与卡片，对齐 generate 工具面的列表和卡片文案。"""
from __future__ import annotations

from pathlib import Path

from xskill.traj_browse import (
    CARDS_MAX,
    LIST_CONTEXT,
    LIST_PAGE,
    find_query_hits,
    format_cards,
    format_listing,
    hit_to_public,
    listing_hit,
    page_slice,
    render_card,
)


def _write(root: Path, traj_id: str, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{traj_id}.md"
    path.write_text(body, encoding="utf-8")
    return path


def _short_card_md(query: str) -> str:
    return (
        "# Trajectory\n\n"
        f"## User\n\n{query}\n\n"
        "## Assistant\n\nI'll look at the cache.\n\n"
        "## Tool Call: Bash\n\ninput: echo pad\n\n"
        "## User\n\n收紧缓存后 RSS 回落\n\n"
        "## Assistant\n\nRSS dropped after the cache tighten.\n\n"
        "padding-line-to-pass-min-traj-bytes\n"
    )


def test_find_query_hits_first_line(tmp_path):
    alice = tmp_path / "alice"
    bob = tmp_path / "bob"
    _write(alice, "traj_cc_alice_memleak", _short_card_md(
        "diagnose a python process leaking memory",
    ))
    _write(bob, "traj_cc_bob_gc", _short_card_md(
        "tighten the cache after rss climbed",
    ))
    _write(
        alice,
        "traj_cc_alice_other",
        "# Trajectory\n\n## User\n\nunrelated invoice check\n\n"
        "## Assistant\n\nok.\n\n"
        "padding-line-to-pass-min-traj-bytes\n",
    )
    hits = find_query_hits(
        "leaking memory",
        dataset_dirs=[("alice", alice), ("bob", bob)],
    )
    assert [hit.traj_id for hit in hits] == ["traj_cc_alice_memleak"]
    assert hits[0].line >= 1
    assert "leaking memory" in hits[0].snippet
    mixed = find_query_hits(
        "cache",
        dataset_dirs=[("alice", alice), ("bob", bob)],
    )
    assert {hit.traj_id for hit in mixed} == {
        "traj_cc_alice_memleak", "traj_cc_bob_gc",
    }


def test_format_listing_matches_product_header():
    hits = [
        {
            "traj_id": "traj_cc_admin_3353bf9f",
            "line": 90,
            "snippet": "patentdagger",
            "hit_count": 1,
            "context": [
                {"line": 87, "hit": False, "text": "prev a"},
                {"line": 88, "hit": False, "text": "prev b"},
                {"line": 89, "hit": False, "text": "prev c"},
                {"line": 90, "hit": True, "text": "patentdagger"},
                {"line": 91, "hit": False, "text": "next a"},
                {"line": 92, "hit": False, "text": "next b"},
                {"line": 93, "hit": False, "text": "next c"},
            ],
        },
        {
            "traj_id": "traj_cc_patentdagger_f620d0a5",
            "line": 4,
            "snippet": "**cwd**: /home/admin/patentdagger",
            "hit_count": 3,
            "context": [
                {"line": 1, "hit": False, "text": "# Trajectory"},
                {"line": 2, "hit": False, "text": ""},
                {"line": 3, "hit": False, "text": "**cwd**:"},
                {"line": 4, "hit": True, "text": "**cwd**: /home/admin/patentdagger"},
                {"line": 5, "hit": False, "text": "next"},
            ],
        },
    ]
    text = format_listing(
        "patentdagger", hits, total=179, page=1, page_size=30,
    )
    assert text.startswith(
        "query='patentdagger' 命中 179 条不同轨迹，展示 2 条"
        "（按相关度排序）。看内容用 --cards，一次最多 8 个 id。"
    )
    assert "还有 177 条未列出，换一组词可以搜到别的。" in text
    assert "下一页：xskill traj search patentdagger --page 2" in text
    assert "traj_cc_admin_3353bf9f" in text
    assert "  L87  prev a" in text
    assert "  L90* patentdagger" in text
    assert "  L93  next c" in text
    assert "traj_cc_patentdagger_f620d0a5（共命中 3 处）" in text
    assert "  L4* **cwd**: /home/admin/patentdagger" in text
    assert "\tL90:" not in text
    assert "首问:" not in text
    assert "traj_cards" not in text


def test_format_listing_empty():
    assert format_listing("内存泄漏", [], total=0, page=1, page_size=30) == (
        "query='内存泄漏' 没有命中。换一组更常见的词。"
    )


def test_page_slice_and_cards_cap():
    items = list(range(35))
    assert page_slice(items, 1, LIST_PAGE) == items[:30]
    assert page_slice(items, 2, LIST_PAGE) == items[30:]
    assert CARDS_MAX == 8
    assert page_slice(items, 1, CARDS_MAX) == items[:8]


def test_render_card_and_format_cards(tmp_path):
    path = _write(
        tmp_path, "traj_cc_patentdagger_43773fc8", _short_card_md(
            "3GPP 全量抓取 · 自动推进 routine",
        ),
    )
    card = render_card(path)
    assert card.startswith("--- traj_cc_patentdagger_43773fc8 ---")
    assert "来源: claude-code" in card
    assert "user 轮: 2" in card
    assert "工具: Bash×1" in card
    assert "问: 3GPP 全量抓取 · 自动推进 routine" in card
    assert "答: I'll look at the cache." in card
    assert "问: 收紧缓存后 RSS 回落" in card
    assert "精读：xskill traj read traj_cc_patentdagger_43773fc8 --offset-start <上面的 L 行号>" in card
    text = format_cards(
        ["traj_cc_patentdagger_43773fc8"],
        dataset_dirs=[("alice", tmp_path)],
        leftover=2,
        query="patentdagger",
        page=1,
        extra_flags="--local",
    )
    assert text.startswith("cards=1（卡片只是索引，不算精读）")
    assert "还有 2 张未列出。" in text
    assert "下一页：xskill traj search patentdagger --cards --page 2 --local" in text
    missing = format_cards(
        ["traj_missing"], dataset_dirs=[("alice", tmp_path)],
    )
    assert "找不到这条轨迹" in missing
    assert "xskill traj search" in missing


def test_listing_hit_includes_three_lines_each_side(tmp_path):
    lines = [f"line-{index}" for index in range(1, 12)]
    lines[6] = "patentdagger lives here"
    path = _write(tmp_path, "traj_cc_alice_memleak", "\n".join(lines) + "\n")
    hits = find_query_hits(
        "patentdagger", dataset_dirs=[("alice", tmp_path)],
    )
    assert hits
    row = listing_hit(hits[0])
    assert LIST_CONTEXT == 3
    assert [item["line"] for item in row["context"]] == [4, 5, 6, 7, 8, 9, 10]
    assert [item["hit"] for item in row["context"]] == [
        False, False, False, True, False, False, False,
    ]
    assert row["context"][3]["text"] == "patentdagger lives here"
    assert row["context"][0]["text"] == "line-4"
    assert row["context"][6]["text"] == "line-10"
    assert str(path) not in str(row)


def test_hit_to_public_drops_path(tmp_path):
    path = _write(tmp_path, "traj_cc_alice_memleak", _short_card_md(
        "diagnose a python process leaking memory",
    ))
    hits = find_query_hits(
        "leaking memory", dataset_dirs=[("alice", tmp_path)],
    )
    public = hit_to_public(hits[0])
    assert public["kind"] == "traj"
    assert public["traj_id"] == "traj_cc_alice_memleak"
    assert public["user"] == "alice"
    assert "path" not in public
    assert str(path) not in str(public)
