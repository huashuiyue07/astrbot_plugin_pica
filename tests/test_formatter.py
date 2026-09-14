"""formatter 消息格式化测试"""

from core.formatter import MessageFormatter


def test_format_search_results_empty():
    out = MessageFormatter.format_search_results([], "keyword")
    assert "未找到" in out


def test_format_search_results_with_items():
    comics = [
        {"title": "测试本子", "_id": "abc", "author": "作者", "totalLikes": 10}
    ]
    out = MessageFormatter.format_search_results(
        comics, "kw", page=1, total=10, page_size=10
    )
    assert "测试本子" in out
    assert "回复 /picainfo <ID>" in out
    assert "已显示全部" in out


def test_format_search_results_pagination_hint():
    comics = [
        {"title": f"本子{i}", "_id": f"id{i}", "totalLikes": i} for i in range(5)
    ]
    out = MessageFormatter.format_search_results(
        comics, "kw", page=1, total=50, page_size=10
    )
    assert "下一页" in out
    assert "共 50 条" in out


def test_help_text():
    text = MessageFormatter.help_text()
    assert "/picalogin" in text
    assert "/picadl" in text
    assert "/picaclean" in text


def test_format_categories():
    out = MessageFormatter.format_categories(["A", "B"])
    assert "A" in out and "B" in out
    assert "/picacomics" in out


def test_format_comic():
    comic = {"title": "T", "_id": "c1", "author": "A", "totalViews": 100,
             "totalLikes": 3, "commentsCount": 1}
    out = MessageFormatter.format_comic(comic, with_episodes=True)
    assert "T" in out and "c1" in out and "A" in out
    assert "/picadl <ID>" in out