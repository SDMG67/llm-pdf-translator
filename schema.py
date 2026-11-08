from pydantic import BaseModel, Field
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Text, String, Integer


class TranslatorRequest(BaseModel):
    original_text: str = Field(...)
    translate_model: str = Field(...)
    language: str = Field(...)


class Base(DeclarativeBase):
    pass


class Translate(Base):
    __tablename__ = "translate"
    translate_model: Mapped[str] = mapped_column(String(20))
    language: Mapped[str] = mapped_column(String(20))
    original_text: Mapped[str] = mapped_column(Text)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class Translate_Cache(Base):
    __tablename__ = "translate_cache"
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    original_text: Mapped[str] = mapped_column(Text)
    translated_text: Mapped[str] = mapped_column(Text)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
