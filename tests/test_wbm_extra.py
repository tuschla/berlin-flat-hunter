"""_fill_wbm_extra: fill WBM extra fields defensively (selects != text inputs)."""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from berlin_flat_hunter.applicator import _fill_wbm_extra  # noqa: E402


def test_select_uses_select_never_clear():
    # Anrede is a <select>; clear()/send_keys on it raises 'invalid element
    # state' and used to crash the whole apply. It must go through Select().
    el = MagicMock()
    el.tag_name = "select"
    el.find_elements.return_value = []  # no <option>s -> select_by_* fails, swallowed
    driver = MagicMock()
    driver.find_element.return_value = el
    _fill_wbm_extra(driver, "anrede", "Herr")  # must not raise
    el.clear.assert_not_called()
    el.send_keys.assert_not_called()


def test_text_input_is_filled():
    el = MagicMock()
    el.tag_name = "input"
    driver = MagicMock()
    driver.find_element.return_value = el
    _fill_wbm_extra(driver, "wbsvorhanden", "ja")
    el.clear.assert_called_once()
    el.send_keys.assert_called_once_with("ja")


def test_empty_value_is_noop():
    driver = MagicMock()
    _fill_wbm_extra(driver, "anrede", "")
    driver.find_element.assert_not_called()


def test_never_raises_on_fill_error():
    el = MagicMock()
    el.tag_name = "input"
    el.clear.side_effect = Exception("invalid element state")
    driver = MagicMock()
    driver.find_element.return_value = el
    _fill_wbm_extra(driver, "x", "y")  # must swallow and not raise
