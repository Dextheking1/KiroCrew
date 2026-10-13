"""The shared shell reader attributes a heredoc body to its consumer, and bounds
its descent on structure rather than on the length of ``$HOME``.

Both behaviours live in ``shell_normalizer._shell_payload_walk``, the one reader
every argv floor consumes, so they are exercised here against the walk directly
with neutral commands.
"""

import os.path

import pytest

from kiro_crew.security import shell_normalizer as sn


def _frames(command: str) -> "list[list[str]]":
    return [tokens for _source, tokens in sn._shell_payload_walk(command)]


def _body_is_read(command: str, *body_words: str) -> bool:
    """True if every *body_words* word appears together in one frame's tokens."""
    return any(all(word in tokens for word in body_words) for tokens in _frames(command))


class TestADataConsumerBodyIsSetAside:
    """A consumer that only READS its heredoc (prints it, writes it, hands it to a
    builtin subcommand) does not run the body, so the body's words are the
    consumer's data and are not folded into the frame as commands."""

    SET_ASIDE = (
        "cat <<eof\nalpha bravo\neof",
        "tee notes.txt <<eof\nalpha bravo\neof",
        "cat > out.txt <<'eof'\nalpha bravo\neof",
        "git commit -f - <<'eof'\nalpha bravo\neof",
        "gh pr create --body \"$(cat <<'eof'\nalpha bravo\neof\n)\"",
    )

    @pytest.mark.parametrize("command", SET_ASIDE)
    def test_the_body_words_are_not_read_as_commands(self, command: str) -> None:
        assert not _body_is_read(command, "alpha", "bravo"), command


class TestABodyTheLineCanRunIsRead:
    """When something in the line runs the text the consumer prints -- a shell or
    evaluator consuming it, a pipe into one, or an expansion whose output is a
    command -- the body is a command and is read as one."""

    READ = (
        "bash <<eof\nalpha bravo\neof",
        "sh <<eof\nalpha bravo\neof",
        "cat <<eof | bash\nalpha bravo\neof",
        "cat <<eof | xargs echo\nalpha bravo\neof",
        "$(cat <<eof\nalpha bravo\neof\n)",
        'eval "$(cat <<eof\nalpha bravo\neof\n)"',
        "m=$(cat <<eof\nalpha bravo\neof\n); $m",
    )

    @pytest.mark.parametrize("command", READ)
    def test_the_body_words_are_read_as_commands(self, command: str) -> None:
        assert _body_is_read(command, "alpha", "bravo"), command


class TestOnlyTheSimpleShapeIsSetAside:
    """Attribution covers the one-heredoc, one-opener-line shape; every other shape
    keeps its body, the fail-closed reading."""

    def test_two_heredocs_on_a_line_keep_their_bodies(self) -> None:
        assert _body_is_read("cat <<a <<b\nalpha\na\nbravo\nb", "alpha")
        assert _body_is_read("cat <<a <<b\nalpha\na\nbravo\nb", "bravo")

    def test_a_substitution_inside_a_data_body_is_still_its_own_frame(self) -> None:
        # ``cat`` only prints the body, but the substitution inside it runs on its
        # own, so its program is still a frame of the walk.
        frames = _frames("cat <<eof\n$(id -u)\neof")
        assert any("id" in tokens for tokens in frames)


class TestTheDescentBoundIsHomeLengthIndependent:
    """A nested payload lives inside one token of its parent, so the walk descends
    into it whatever the length of the home path -- the bound is structural, not a
    function of ``$HOME``."""

    def _set_home(self, monkeypatch: pytest.MonkeyPatch, home: str) -> None:
        real = os.path.expanduser

        def fake(path: str) -> str:
            return home + path[1:] if path.startswith("~") else real(path)

        monkeypatch.setattr(os.path, "expanduser", fake)

    @pytest.mark.parametrize("home", ["/srv/u", "/srv/" + "x" * 200])
    def test_a_nested_payload_is_descended_for_any_home(
        self, monkeypatch: pytest.MonkeyPatch, home: str
    ) -> None:
        self._set_home(monkeypatch, home)
        command = "sh -c \"ls '$home'\""
        # The inner ``ls`` command is a frame of its own, not only the outer ``sh``.
        assert any(tokens and tokens[0] == "ls" for tokens in _frames(command))

    def test_the_walk_terminates_on_deep_nesting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_home(monkeypatch, "/srv/" + "x" * 200)
        command = 'sh -c "sh -c \\"ls \'$home\'\\""'
        assert len(_frames(command)) >= 2
