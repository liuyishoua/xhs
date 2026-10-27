"""Recipient verification must retain final pages and reject incomplete reads."""
from types import SimpleNamespace

import pytest

from apis.xhs_pc_apis import XHS_Apis


@pytest.mark.parametrize('inner', [False, True])
@pytest.mark.parametrize('payload,expected', [
    ({'success': True, 'code': 0, 'data': {'comments': [], 'has_more': False}}, True),
    ({'success': True, 'code': 0, 'data': {}}, False),
    ({'success': True, 'data': {'comments': []}}, False),
    ({'success': False, 'code': -1, 'data': {}}, False),
])
def test_comment_pages_allow_missing_message_but_require_complete_data(inner, payload, expected):
    api = XHS_Apis.__new__(XHS_Apis)
    api.base_url = 'https://example.test'
    api._request_params = lambda *args: ({}, {}, '')
    api._proxies = lambda *args: None
    api.http = SimpleNamespace(get=lambda *args, **kwargs: SimpleNamespace(json=lambda: payload))
    if inner:
        success, _, raw = api.get_note_inner_comment({'id': 'root', 'note_id': 'note'}, '', 'synthetic')
    else:
        success, _, raw = api.get_note_out_comment('note', '', 'synthetic')
    assert success is expected
    assert raw == payload


def test_outer_final_page_without_cursor_keeps_comments():
    api = XHS_Apis.__new__(XHS_Apis)
    pages = iter([
        (True, '', {'data': {'comments': [{'id': 'first'}], 'has_more': True, 'cursor': 'next'}}),
        (True, '', {'data': {'comments': [{'id': 'last'}], 'has_more': False}}),
    ])
    cursors = []

    def fetch(note, cursor, *args):
        cursors.append(cursor)
        return next(pages)

    api.get_note_out_comment = fetch
    success, _, rows = api.get_note_all_out_comment('note', 'synthetic')
    assert success is True
    assert rows == [{'id': 'first'}, {'id': 'last'}]
    assert cursors == ['', 'next']


def test_inner_final_page_without_cursor_keeps_preloaded_and_fetched_replies():
    api = XHS_Apis.__new__(XHS_Apis)
    root = {'id': 'root', 'sub_comment_has_more': True, 'sub_comment_cursor': 'start',
            'sub_comments': [{'id': 'preloaded'}]}
    pages = iter([
        (True, '', {'data': {'comments': [{'id': 'middle'}], 'has_more': True, 'cursor': 'last'}}),
        (True, '', {'data': {'comments': [{'id': 'final'}], 'has_more': False}}),
    ])
    api.get_note_inner_comment = lambda *args: next(pages)
    success, _, result = api.get_note_all_inner_comment(root, 'synthetic')
    assert success is True
    assert result['sub_comments'] == [{'id': 'preloaded'}, {'id': 'middle'}, {'id': 'final'}]


@pytest.mark.parametrize('inner', [False, True])
def test_repeating_cursor_fails_without_looping_or_claiming_complete_read(inner):
    api = XHS_Apis.__new__(XHS_Apis)
    calls = []

    def fetch(*args):
        calls.append(True)
        assert len(calls) == 1
        return True, '', {'data': {'comments': [], 'has_more': True, 'cursor': 'start' if inner else ''}}

    if inner:
        api.get_note_inner_comment = fetch
        result = api.get_note_all_inner_comment({'sub_comment_has_more': True, 'sub_comment_cursor': 'start', 'sub_comments': []}, 'synthetic')
    else:
        api.get_note_out_comment = fetch
        result = api.get_note_all_out_comment('note', 'synthetic')
    assert result[0] is False
    assert '分页游标未推进' in result[1]


@pytest.mark.parametrize('inner', [False, True])
def test_incomplete_success_is_not_treated_as_an_empty_finished_page(inner):
    api = XHS_Apis.__new__(XHS_Apis)
    fetch = lambda *args: (True, '', {'success': True, 'code': 0, 'data': {}})
    if inner:
        api.get_note_inner_comment = fetch
        result = api.get_note_all_inner_comment({'sub_comment_has_more': True, 'sub_comment_cursor': 'start', 'sub_comments': []}, 'synthetic')
    else:
        api.get_note_out_comment = fetch
        result = api.get_note_all_out_comment('note', 'synthetic')
    assert result[0] is False
    assert '不能确认原帖可见性' in result[1]
