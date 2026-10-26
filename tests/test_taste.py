from pathlib import Path

import pytest

from sfx_lab.lab import Lab, atomic_write, read_text
from sfx_lab.taste import (END, START, agent_part, clean_agent_markdown, read_taste,
                           write_auto)


def test_write_preserves_outside_block_byte_for_byte(lab: Lab) -> None:
    text = ('---\r\ncreated: 2000-01-01\r\nmodified: 1900-01-01\r\nratings: 0\r\n---\r\n'
            '# Custom title\r\nBefore café\r\n' + START + '\r\nold\r\n' + END +
            '\r\n\r\n## My notes\r\n  Keep these spaces.  \r\n\r\n')
    atomic_write(lab.taste_path, text)
    write_auto(lab.taste_path, "## Likes\nclicks\n## Dislikes\nnoise", "best", "worst", "numbers", 3)
    result = read_text(lab.taste_path)
    prefix, suffix = text.split(START)[0], text.split(END)[1]
    assert result.split(START)[0].replace(result.split("modified: ")[1].split("\r")[0], "1900-01-01").replace("ratings: 3", "ratings: 0") == prefix
    assert result.split(END)[1] == suffix
    assert "ratings: 3\r\n" in result
    taste = read_taste(lab.taste_path)
    assert "## Numbers\nnumbers" in taste.auto
    assert taste.notes == suffix


@pytest.mark.parametrize("original", ["# Profile\n\n## My notes\nMine!\n", "# Profile\nMine!\n",
                                      "# Profile\n" + START + "\nold\n## My notes\nMine!\n",
                                      "# Profile\n" + END + "\nold\n## My notes\nMine!\n"])
def test_missing_markers_recovered(tmp_path: Path, original: str) -> None:
    path = tmp_path / "taste.md"
    atomic_write(path, original)
    write_auto(path, "## Likes\nclick\n## Dislikes\nbuzz", "best", "worst", "numbers", 1)
    result = read_text(path)
    assert START in result and END in result
    assert "Mine!\n" in result
    if "## My notes" in original:
        assert result.index(END) < result.index("## My notes")
        assert result[result.index("## My notes"):] == original[original.index("## My notes"):]
    assert "## Likes\nclick" in read_taste(path).auto
    write_auto(path, "## Likes\nnew click\n## Dislikes\nbuzz", "best", "worst", "numbers", 2)
    assert read_taste(path).auto.startswith("## Likes\nnew click")


def test_strip_agent_title_and_fences() -> None:
    value = clean_agent_markdown("```markdown\n# Title\n\n## Likes\nclick\n## Dislikes\nbuzz\n```")
    assert value == "## Likes\nclick\n## Dislikes\nbuzz"
    assert agent_part(value + "\n\n## Best examples\nold") == value
