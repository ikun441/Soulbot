"""Fixed card templates. Model text is data, never HTML, CSS or script."""

from html import escape
from typing import Literal

from pydantic import Field, field_validator, model_validator

from soulbot.services.config_service import Model

FONT_SHA256 = "ae82f4e2a55e1316a55bcc1d05e9555ce08d8bda07e893b486896b626fd852ff"
FONT_URL = "https://raw.githubusercontent.com/MemeCrafters/meme-generator/v0.1.14/resources/fonts/NotoSansSC-Regular.ttf"


class CardRequest(Model):
    template: Literal["note", "quote", "list"] = "note"
    theme: Literal["pink", "lilac", "mint"] = "pink"
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(default="", max_length=1200)
    items: list[str] = Field(default_factory=list, max_length=12)
    footer: str = Field(default="SoulBot", max_length=100)

    @field_validator("title")
    @classmethod
    def title_not_empty(cls, value):
        if not value.strip():
            raise ValueError("请填写卡片标题")
        return value.strip()

    @field_validator("items")
    @classmethod
    def bounded_items(cls, values):
        if any(not item.strip() or len(item) > 120 for item in values):
            raise ValueError("清单项目不能为空，每项最多 120 字")
        return [item.strip() for item in values]

    @model_validator(mode="after")
    def template_content(self):
        if self.template == "list":
            if not self.items:
                raise ValueError("清单卡片至少需要一项内容")
        elif not self.body.strip() or self.items:
            raise ValueError("文字/引用卡片需要正文，清单项目仅用于清单模板")
        return self


def card_html(card: CardRequest, font_base64: str):
    colors = {
        "pink": ("#fff6fa", "#f8dce9", "#a33a69"),
        "lilac": ("#faf7ff", "#e4dcf8", "#6c4e9a"),
        "mint": ("#f4fdf9", "#d7eee4", "#33755e"),
    }
    background, glow, accent = colors[card.theme]
    body = f'<p class="body">{escape(card.body)}</p>' if card.body else ""
    if card.template == "list":
        body += "<ol>" + "".join(f"<li>{escape(item)}</li>" for item in card.items) + "</ol>"
    if card.template == "quote":
        body = '<div class="quote-mark" aria-hidden="true">“</div>' + body
    label = {"note": "A LITTLE NOTE", "quote": "WORDS TO KEEP", "list": "ONE THING AT A TIME"}[
        card.template
    ]
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; form-action 'none'">
<style>
@font-face {{font-family:SoulBotNoto;src:url(data:font/ttf;base64,{font_base64}) format('truetype');font-weight:400;font-display:block}}
*{{box-sizing:border-box}}html,body{{margin:0;background:transparent;font-family:SoulBotNoto,sans-serif;color:#40353f}}
#card{{width:100%;padding:52px 56px 40px;border-radius:30px;background:linear-gradient(145deg,{background}, {glow});border:1px solid #ffffffd0;overflow-wrap:anywhere}}
.eyebrow{{font:12px sans-serif;letter-spacing:3px;color:{accent};margin-bottom:22px}}
h1{{font-size:34px;font-weight:400;line-height:1.5;margin:0 0 24px;color:{accent}}}
.body{{white-space:pre-wrap;font-size:23px;line-height:1.85;margin:0}}
.quote-mark{{font:86px Georgia,serif;height:70px;color:{accent};opacity:.35}}
ol{{list-style:none;padding:0;counter-reset:item;margin:20px 0 0}}li{{counter-increment:item;display:flex;gap:16px;font-size:22px;line-height:1.8;padding:15px 0;border-bottom:1px solid #ffffffa0}}
li::before{{content:counter(item,decimal-leading-zero);font:14px sans-serif;color:{accent};padding-top:12px;flex-shrink:0}}
footer{{margin-top:36px;padding-top:20px;border-top:1px solid #ffffffb0;font-size:15px;line-height:1.8;color:{accent};white-space:pre-wrap}}
</style></head><body><article id="card"><div class="eyebrow">{label}</div><h1>{escape(card.title)}</h1>{body}<footer>{escape(card.footer)}</footer></article></body></html>"""
