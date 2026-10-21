"""O protagonista do modal não vira NPC e o plot não o esquece."""

from app.assistant import bind_protagonist


def test_protagonist_card_is_removed_and_the_plot_names_them():
    draft = {
        "plot_essentials": "Um reino de cristal.",
        "story_cards": [
            {"title": "Alusair", "type": "character", "entry": "o jogador"},
            {"title": "Alusair, a exilada", "type": "character", "entry": "ainda o jogador"},
            {"title": "Elara", "type": "character", "entry": "a rainha"},
        ],
    }
    bind_protagonist(
        draft,
        name="Alusair",
        role="cavaleiro exilado",
        appearance="cabelo preto curto",
        description="Quer o selo do rei.",
    )
    titles = [card["title"] for card in draft["story_cards"]]
    assert titles == ["Elara"]
    plot = draft["plot_essentials"]
    assert plot.startswith("Protagonista — ")
    assert "Alusair" in plot
    assert "cavaleiro exilado" in plot
    assert "cabelo preto curto" in plot
    assert "selo do rei" in plot
    assert plot.endswith("Um reino de cristal.")


def test_existing_plot_is_not_repeated():
    draft = {
        "plot_essentials": "Nome: Alusair. Papel: barda.",
        "story_cards": [],
    }
    bind_protagonist(draft, name="Alusair", role="barda", appearance="", description="")
    assert draft["plot_essentials"] == "Nome: Alusair. Papel: barda."


def test_description_alone_opens_the_plot():
    draft = {"plot_essentials": "", "story_cards": []}
    bind_protagonist(draft, description="Uma maga que não dorme há três noites.")
    assert draft["plot_essentials"].startswith("Protagonista — Uma maga")
