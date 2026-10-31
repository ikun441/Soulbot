"""Validated plugin manifest and catalog; parsing never imports plugin Python."""

import math
import re
from pathlib import PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from soulbot.services.config_service import Model

PLUGIN_ID = r"^[a-z][a-z0-9_]{0,39}$"
MAX_FILE = 2 * 1024 * 1024
MAX_PACKAGE = 10 * 1024 * 1024


def relative_file(value):
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or "\\" in value
        or ":" in value
        or any(p in {".", ".."} or p.startswith(".") for p in path.parts)
    ):
        raise ValueError("插件文件路径无效")
    return value


class UniqueLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("插件配置不接受 YAML 别名")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("插件配置包含重复或非文本键")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


class Definition(Model):
    name: str = Field(min_length=1, max_length=80)
    author: str = Field(min_length=1, max_length=80)
    version: str = Field(pattern=r"^v\d+\.\d+\.\d+$")
    info: str = Field(min_length=1, max_length=1000)

    @field_validator("name", "author", "info")
    @classmethod
    def normal_text(cls, value):
        if not value.strip() or any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ValueError("插件说明须为正常文本")
        return value.strip()


class Parameter(Model):
    type: Literal["string", "integer", "number", "boolean", "enum"]
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    default: Any
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)
    max_length: int = Field(default=1000, ge=1, le=16384)
    options: list[str] = Field(default_factory=list, max_length=30)
    secret: bool = False

    def checked(self, value):
        if self.type == "string":
            if not isinstance(value, str) or len(value) > self.max_length or "\x00" in value:
                raise ValueError("插件文本参数不符合要求")
        elif self.type == "boolean":
            if type(value) is not bool:
                raise ValueError("插件布尔参数不符合要求")
        elif self.type in {"integer", "number"}:
            if type(value) not in (
                {int} if self.type == "integer" else {int, float}
            ) or not math.isfinite(value):
                raise ValueError("插件数值参数不符合要求")
            if (
                self.minimum is not None
                and value < self.minimum
                or self.maximum is not None
                and value > self.maximum
            ):
                raise ValueError("插件参数超出允许范围")
        elif not isinstance(value, str) or value not in self.options:
            raise ValueError("插件选项不符合要求")
        return value

    @model_validator(mode="after")
    def valid(self):
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("插件参数边界顺序不正确")
        if (
            any(not v or len(v) > 120 for v in self.options)
            or self.type == "enum"
            and not self.options
        ):
            raise ValueError("插件枚举选项无效")
        if self.secret and (self.type != "string" or self.default != ""):
            raise ValueError("插件密钥只能是默认留空的字符串")
        self.checked(self.default)
        return self


class Manifest(Model):
    api_version: Literal[1]
    define: Definition
    parameters: dict[str, Parameter] = Field(default_factory=dict, max_length=50)
    config: dict[str, Any] = Field(default_factory=dict, max_length=50)

    @field_validator("api_version", mode="before")
    @classmethod
    def integer_api(cls, value):
        if type(value) is not int:
            raise ValueError("插件 api_version 必须是整数")
        return value

    @model_validator(mode="after")
    def valid(self):
        if (
            any(not re.fullmatch(PLUGIN_ID, k) for k in self.parameters)
            or not self.config.keys() <= self.parameters.keys()
        ):
            raise ValueError("插件参数名称无效或没有声明")
        for name, value in self.config.items():
            field = self.parameters[name]
            if field.secret:
                raise ValueError("插件包中不能保存密钥")
            field.checked(value)
        return self

    @classmethod
    def parse(cls, content):
        if len(content) > 65536:
            raise ValueError("插件 config.yaml 超过64KiB")
        return cls.model_validate(yaml.load(content.decode("utf-8-sig"), Loader=UniqueLoader))


class CatalogEntry(Definition):
    id: str = Field(pattern=PLUGIN_ID)
    path: str = Field(max_length=120)
    files: dict[str, str] = Field(min_length=4, max_length=100)

    @model_validator(mode="after")
    def package(self):
        if (
            self.path != "plugins/" + self.id
            or not {"config.yaml", "plugin.py", "README.md", "LICENSE"} <= self.files.keys()
        ):
            raise ValueError("插件目录或必需文件无效")
        for name, digest in self.files.items():
            relative_file(name)
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("插件文件摘要无效")
        return self


class Catalog(Model):
    format: Literal[1]
    plugins: list[CatalogEntry] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def distinct(self):
        if len({p.id for p in self.plugins}) != len(self.plugins):
            raise ValueError("索引包含重复插件ID")
        return self
