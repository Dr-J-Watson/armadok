"""Free text in a notification is made harmless for each kind of channel."""

from armadok.core.module import NotificationEvent
from armadok.modules.notifications import delivery


def test_a_mention_cannot_ping_anybody_in_any_external_channel():
    for kind in ("discord", "ntfy", "email", "webhook"):
        out = delivery.neutralise("hello @everyone and @here <@123>", kind)
        assert "@everyone" not in out and "@here" not in out and "<@" not in out
        assert out.replace(delivery.ZERO_WIDTH, "").replace("\\", "") == (
            "hello @everyone and @here <@123>"
        )


def test_an_address_in_the_middle_of_a_word_is_left_alone():
    assert delivery.neutralise("ops@example.com", "email") == "ops@example.com"


def test_markdown_is_escaped_only_for_discord():
    assert delivery.neutralise("**big** [x](y)", "discord").count("\\") == 8
    assert delivery.neutralise("**big** [x](y)", "ntfy") == "**big** [x](y)"


def test_render_neutralises_values_for_a_channel_but_not_for_the_inbox():
    event = NotificationEvent(
        name="t.x",
        severity="info",
        right=None,
        templates={"en": ("Machine {name}", "{name} is down")},
    )
    inbox = delivery.render(event, {"name": "@everyone"}, "en")
    assert inbox == ("Machine @everyone", "@everyone is down")
    title, body = delivery.render(event, {"name": "@everyone"}, "en", "discord")
    assert "@everyone" not in title + body
