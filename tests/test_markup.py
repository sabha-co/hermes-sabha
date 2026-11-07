from markup import strip_mention, to_html


def test_markdown_renders_and_raw_html_is_escaped():
    html = to_html("**Deployed** `v2.3`\n\n<script>alert(1)</script>")
    assert "<strong>Deployed</strong>" in html and "<code>v2.3</code>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_mentions_survive_and_images_become_links():
    html = to_html("ping @{42} ![chart](https://example.com/c.png)")
    assert "@{42}" in html
    assert '<a href="https://example.com/c.png">chart</a>' in html and "<img" not in html


def test_task_list_boxes_render_as_symbols():
    html = to_html("- [ ] back up\n- [x] run tests\n\nleave [ ] alone mid-line")
    assert "<li>☐ back up</li>" in html and "<li>☑ run tests</li>" in html
    assert "leave [ ] alone" in html


def test_strip_mention_handles_names_with_spaces_and_leaves_others():
    assert strip_mention("@Deploy Bot ship it", "Deploy Bot") == "ship it"
    assert strip_mention("hey @Hermes, and @Hermesian", "hermes") == "hey , and @Hermesian"
