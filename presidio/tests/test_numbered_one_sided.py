"""One-sided numbered containers: only a window-cut side reaches the paragraph edge."""
from presidio.ner.boundary_values import BoundaryValues


def numbered(body, eol='\n'):
    lines = ['header {metadata}', '1: Документ', '2:',
             *[f'{n}: {line}' for n, line in enumerate(body.split('\n'), 3)],
             f'{3 + len(body.split(chr(10)))}:', 'tail [metadata]']
    return eol.join(lines)


def cover(text, bounds, value):
    a = text.index(value); return bounds[0] <= a and a + len(value) <= bounds[1]


def test_both_open_is_unchanged_paragraph():
    text = numbered('Иван Петров работает тут')
    a = text.index('Петров')
    assert BoundaryValues(text).resolve(a, a + 3) == BoundaryValues(text).resolve(a, a + 3, left_open=True, right_open=True)


def test_left_confirmed_mid_entity_still_covers_legal_form_and_wrapped_tail():
    for eol in ['\n', '\r\n', '\r', ' ', ' ']:
        text = numbered('Контрагент ООО Янтарный\nПеревал и далее', eol)
        a = text.index('Янтарный')
        b = BoundaryValues(text).resolve(a, a + 3, left_open=False, right_open=True)
        assert cover(text, b, 'ООО Янтарный'), (eol, text[slice(*b)])
        assert cover(text, b, 'Перевал')


def test_left_confirmed_stops_at_parenthesis_only():
    text = numbered('Общее правило важно. Код (оверлей\nAndroid: подробности).')
    a = text.index('оверлей') + 3
    b = BoundaryValues(text).resolve(a, a + 4, left_open=False, right_open=True)
    assert text[b[0]:b[0] + 7] == 'оверлей' and 'Общее правило' not in text[slice(*b)]
    assert text[slice(*b)].endswith('подробности).')


def test_initials_and_inverted_names_are_not_split():
    for body, value, frag in [('Подписал Иванов И. И. вчера', 'Иванов И. И.', 'И. вчера'),
                              ('Список: Петров, Иван Сергеевич и др', 'Петров, Иван', 'Иван'),
                              ('Партнёр ООО «Хвойный Мыс» сообщил', 'ООО «Хвойный Мыс»', 'Мыс')]:
        text = numbered(body)
        a = text.index(frag)
        b = BoundaryValues(text).resolve(a, a + 2, left_open=False, right_open=True)
        assert cover(text, b, value), (body, text[slice(*b)])


def test_right_confirmed_symmetry():
    text = numbered('Начало (ООО Янтарный\nПеревал) и хвост — конец')
    a = text.index('Перевал')
    b = BoundaryValues(text).resolve(a, a + 7, left_open=True, right_open=False)
    assert cover(text, b, 'ООО Янтарный') and cover(text, b, 'Перевал')
    assert 'хвост' not in text[slice(*b)]


def test_rejections_unchanged():
    # Grammar refusals of the candidate are not bypassed by closed sides.
    for body in ['Иван "Петров"', 'x = Иван Петров', 'Иван {Петров}']:
        text = numbered(body)
        a = text.index('Петров')
        assert BoundaryValues(text).resolve(a, a + 3, left_open=False, right_open=True) is None
    clipped = 'header {meta}\n1: Документ\n2:\n3: Иван Петров'
    a = clipped.index('Петров')
    assert BoundaryValues(clipped).resolve(a, a + 3, left_open=False, right_open=True) is None


def test_container_never_leaves_paragraph():
    text = numbered('Иван Петров; работает')
    a = text.index('Иван')
    full = BoundaryValues(text).resolve(a, a + 4)
    b = BoundaryValues(text).resolve(a, a + 4, left_open=False, right_open=False)
    assert full[0] <= b[0] < b[1] <= full[1] and text[slice(*b)] == 'Иван Петров'
