import pytest

from bot.memory.store import Store


@pytest.fixture
async def store(tmp_path):
    """Чистая база во временной папке для каждого теста."""
    s = Store(tmp_path / "test.db")
    await s.open()
    yield s
    await s.close()
