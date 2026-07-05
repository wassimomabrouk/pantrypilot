import os
import tempfile
import pytest

# We set PANTRY_DB to a temporary database file before importing pantry_mcp.server,
# to avoid modifying the real pantry.db and to ensure _init_db runs cleanly.
tmp_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp_db_path = tmp_db_file.name
tmp_db_file.close()

os.environ["PANTRY_DB"] = tmp_db_path

from pantry_mcp.server import (
    _valid_name,
    _valid_quantity,
    _valid_unit,
    save_meal_plan,
    MAX_NAME_LEN,
    MAX_QUANTITY,
    ALLOWED_UNITS,
)

# ---------------------------------------------------------------------------
# Tests for _valid_name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name, expected",
    [
        ("Apple", "Apple"),
        ("Olive Oil (Extra Virgin)", "Olive Oil (Extra Virgin)"),
        ("Sugar - 50%", "Sugar - 50%"),
        ("Milk & Honey", "Milk & Honey"),
        ("Salt, Pepper, and Oregano", "Salt, Pepper, and Oregano"),
        ("100% juice", "100% juice"),
        ("onion/garlic", "onion/garlic"),
        ("  Flour  ", "Flour"),  # test stripping leading/trailing whitespace
        ("a" * MAX_NAME_LEN, "a" * MAX_NAME_LEN),  # boundary condition: exactly max len
    ],
)
def test_valid_name(name, expected):
    assert _valid_name(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "",  # empty string
        "   ",  # spaces only
        None,  # None value (handled as empty string)
        "a" * (MAX_NAME_LEN + 1),  # boundary condition: too long
        "Apples!",  # exclamation mark not in allowed regex
        "Sugar#1",  # hash not in allowed regex
        "salt*",  # asterisk not in allowed regex
        "oil@olive",  # @ not in allowed regex
    ],
)
def test_invalid_name(name):
    with pytest.raises(ValueError, match="Invalid item name"):
        _valid_name(name)


# ---------------------------------------------------------------------------
# Tests for _valid_quantity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "q, expected",
    [
        (1, 1.0),
        (0.5, 0.5),
        (10000, 10000.0),  # boundary condition: exactly MAX_QUANTITY
        ("2.5", 2.5),  # string float
        ("5", 5.0),  # string int
    ],
)
def test_valid_quantity(q, expected):
    assert _valid_quantity(q) == expected


@pytest.mark.parametrize(
    "q",
    [
        0,  # boundary condition: zero (must be > 0)
        -1,  # negative
        -0.001,  # tiny negative
        10000.1,  # boundary condition: slightly above MAX_QUANTITY
        15000,  # well above MAX_QUANTITY
        "abc",  # non-numeric string
        None,  # None type
        [],  # list type
    ],
)
def test_invalid_quantity(q):
    with pytest.raises(ValueError) as excinfo:
        _valid_quantity(q)
    assert "Quantity" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Tests for _valid_unit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "unit, expected",
    [
        ("g", "g"),
        ("kg", "kg"),
        ("ml", "ml"),
        ("l", "l"),
        ("piece", "piece"),
        ("pack", "pack"),
        ("can", "can"),
        ("jar", "jar"),
        ("bottle", "bottle"),
        ("  G  ", "g"),  # test whitespace stripping and lowercase conversion
        ("Bottle", "bottle"),  # test lowercase conversion
    ],
)
def test_valid_unit(unit, expected):
    assert _valid_unit(unit) == expected


@pytest.mark.parametrize(
    "unit",
    [
        "tbsp",
        "cup",
        "ounces",
        "",
        "   ",
        None,
    ],
)
def test_invalid_unit(unit):
    with pytest.raises(ValueError, match="Unit must be one of"):
        _valid_unit(unit)


# ---------------------------------------------------------------------------
# Tests for save_meal_plan JSON validation
# ---------------------------------------------------------------------------

def test_save_meal_plan_valid_json():
    # Valid week label and valid JSON string
    week_label = "Week 26"
    valid_json = '{"Monday": {"dinner": "Couscous", "ingredients": ["semolina", "vegetables"]}}'
    
    result = save_meal_plan(week_label, valid_json)
    assert f"Meal plan for '{week_label}' saved." in result


@pytest.mark.parametrize(
    "invalid_json",
    [
        '{"Monday": {"dinner": "Couscous"}',  # missing closing brace
        "not a json string",  # plain text
        "",  # empty string
        "   ",  # spaces only
        "{'Monday': 'Couscous'}",  # single quotes (invalid JSON standard)
    ],
)
def test_save_meal_plan_invalid_json(invalid_json):
    week_label = "Week 26"
    
    with pytest.raises(ValueError) as excinfo:
        save_meal_plan(week_label, invalid_json)
        
    err_msg = str(excinfo.value)
    assert ("JSON must be 1-4000 characters" in err_msg) or ("plan_json is not valid JSON" in err_msg)


def test_save_meal_plan_invalid_week_label():
    valid_json = '{"Monday": "Pasta"}'
    
    # Test empty week label
    with pytest.raises(ValueError, match="Week label must be 1-4000 characters"):
        save_meal_plan("", valid_json)
        
    # Test None week label
    with pytest.raises(ValueError, match="Week label must be 1-4000 characters"):
        save_meal_plan(None, valid_json)


def test_save_meal_plan_too_long_inputs():
    valid_json = '{"Monday": "Pasta"}'
    
    # 4001 characters long strings
    long_str = "a" * 4001
    
    # Test week label too long
    with pytest.raises(ValueError, match="Week label must be 1-4000 characters"):
        save_meal_plan(long_str, valid_json)
        
    # Test plan JSON too long
    with pytest.raises(ValueError, match="Plan JSON must be 1-4000 characters"):
        save_meal_plan("Week 26", long_str)


@pytest.fixture(scope="session", autouse=True)
def cleanup_db():
    yield
    try:
        os.unlink(tmp_db_path)
        for ext in ("-wal", "-shm"):
            extra_file = tmp_db_path + ext
            if os.path.exists(extra_file):
                os.unlink(extra_file)
    except OSError:
        pass
