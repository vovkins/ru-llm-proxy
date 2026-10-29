"""Generate the fixed E9 paired report from frozen inputs, without inference."""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from presidio.evaluation.corpus import CorpusCase, EntitySpan, TARGET_ENTITY_TYPES
from presidio.evaluation.tiny2_comparison import ComparisonCase, score_results, write_comparison
from presidio.entity_types import SUPPORTED_ENTITY_TYPES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-cases", required=True, type=Path)
    parser.add_argument("--raw-report-dir", required=True, type=Path)
    parser.add_argument("--reference-source", required=True, type=Path)
    args = parser.parse_args()
    OUT = args.raw_report_dir.resolve()
    packet=json.loads(args.input_cases.read_text())
    cases=[]
    for row in packet['cases']:
        values=dict(row['case'])
        values['expected']=tuple(EntitySpan(**e) for e in values['expected'])
        values['tags']=tuple(values['tags'])
        cases.append(ComparisonCase(CorpusCase(**values),row['corpus'],frozenset(row['scope']),row['complete_annotation'],row['origin']))
    by_id={item.case.case_id:item for item in cases}
    source=packet['source']
    result={'status':'comparison_complete_not_owner_acceptance', 'source':source,
            'reference_revision':'1c8c78d049a06c2de5c198cb07cb6235df494460',
            'candidate_head':packet['git']['head'],'candidate_worktree_dirty':True,
            'evaluator_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in (ROOT/'presidio/evaluation').glob('tiny2_comparison*.py')},
            'modes':{},'qualifications':[
                'No acceptance thresholds; owner decides after quality and performance results.',
                'NER means the recognizer including its production windowing/postprocessing, not raw logits.',
                'Reference full spaCy versus candidate light spaCy; component attribution is not isolated by this table.',
                'P/R/F1 is exact type and offsets within annotation scope; union/whitespace-aware coverage is separate.',
                'DKB strict type acceptance uses original alternative-label manifest in integration tests; exact metrics here use first listed canonical label.',
                'Partially annotated text is not universally clean; extra characters are annotation-scope counts, not automatically false positives.',
                'PII-Bench already used by author; no independent held-out model-selection claim.',
                'Local diagnostic durations are not a performance/capacity benchmark (E10).',
            ]}
    raw={}
    for mode in ('analyzer','ner'):
        raw[mode]={}
        metadata={}
        for system in ('reference','candidate'):
            data=json.loads((OUT/f'{system}-{mode}.json').read_text())
            assert data.get('complete') and data['source']==source and len(data['results'])==len(cases)
            assert len(data['manifest'])==len(cases)
            assert all(v['status']=='ok' for v in data['results'].values())
            expected_root=args.reference_source / 'presidio' if system=='reference' else ROOT/'presidio'
            mismatches=[p for p,sha in data['runtime']['sources_sha256'].items()
                        if hashlib.sha256((expected_root/p).read_bytes()).hexdigest()!=sha]
            assert not mismatches,(system,mismatches)
            state=json.loads((OUT/f'{system}-{mode}-state.json').read_text())
            assert state['status']=='completed' and state['cleanup_ok']
            raw[mode][system]=data['results']
            metadata[system]={'runtime':data['runtime'],'health_before':data['health_before'],
                              'health_after':data['health_after'],'images':state['images'],
                              'trusted_model':state.get('trusted_model'),'source_matches_checkout':True,
                              'request_errors':0,'successful_requests':len(cases),'cleanup_ok':True}
        temp=OUT/f'{mode}-comparison.json'
        result['modes'][mode]=write_comparison(temp,cases,raw[mode]['reference'],raw[mode]['candidate'],metadata)

    def metric(row,key):
        value=row.get(key)
        return '—' if value is None else f'{100*value:.1f}'

    def covered(row):
        return f"{row.get('significant_fully_covered',0)}/{row.get('gold_values_all_requests',0)}"

    def class_table(group,mode='analyzer',kinds=None):
        rows=result['modes'][mode]['groups'][group]
        left,right=(rows[system]['per_entity'] for system in ('reference','candidate'))
        kinds=sorted(kinds or set(left)|set(right))
        lines=['| Класс | Значений | P/R/F1 BERT, % | P/R/F1 Tiny2, % | Защищено BERT | Защищено Tiny2 |',
               '|---|---:|---:|---:|---:|---:|']
        for kind in kinds:
            a,b=left.get(kind,{}),right.get(kind,{})
            label=kind
            if kind=='CVC_OUT_OF_SCOPE':
                label='CVC: вне объёма'
            lines.append(f"| {label} | {a.get('gold_values_all_requests',0)} | "
                         + '/'.join(metric(a,k) for k in ('precision','recall','f1'))+' | '
                         + '/'.join(metric(b,k) for k in ('precision','recall','f1'))
                         + f' | {covered(a)} | {covered(b)} |')
        return lines

    lines=['# Э9: качество BERT и Tiny2/Triton','',
           'Сравнение для решения владельца, не автоматическая приёмка миграции. Порогов допустимого ухудшения нет. '
           'Основной `main` не изменён; кандидат остаётся незакоммиченной рабочей копией PR #109.','',
           '**Переход не принят: есть подтверждённые пропуски и непройденные строгие проверки.** '
           'У Tiny2 62 примера с улучшением покрытия, 7 с ухудшением в согласованных классах '
           'и ещё 6 только по исключённому CVC. Сквозная диагностика подтвердила передачу открытых '
           'фрагментов тестовому провайдеру; успешные технические наборы этого не отменяют.','',
           '## Условия','',
           '- Эталон: свежая сборка `main@1c8c78d`; `fef2/ner_rus_bert-secret_detection@52b5b074`, полный spaCy.',
           '- Кандидат: `Katerina-Bir/rubert-tiny2-secret-detection@4137142e`, проверенный FP32 ONNX/Triton CPU, облегчённый spaCy. '
           'Пороги и поправки не подбирались по результатам Э9.',
           f"- {source['case_count']} записей, {source['unique_texts']} уникальных текстов, {source['duplicate_text_occurrences']} повторных вхождения; "
           '16 групп. Четыре последовательных прогона: 9272 успешных ответа, 0 ошибок запросов/таймаутов. '
           'NER отдельно означает штатный распознаватель с окнами и постобработкой, без правил и общего слияния.',
           '- Порог берётся из исходного корпуса (обычно 0.35). Параметры, исходники, пакеты и контрольные суммы сохранены в '
           '[машинном отчёте](../../presidio/evaluation/reports/tiny2-quality-comparison.json). '
           'Совпадение исполняемого кода с соответствующим checkout проверено.',
           '- Эталон: 4 CPU/4 GiB; кандидат: Analyzer 2 CPU/2 GiB + Triton 2 CPU/2 GiB. '
           'Это ограничения исследовательских контейнеров, не рекомендации промышленной ёмкости; нагрузка относится к Э10.','',
           '## Как читать показатели','',
           '`P/R/F1` требуют точного класса и границ. «Защищено» означает, что объединение любых масок закрывает все '
           'непробельные символы значения; покрытие пробелов и правильным классом сохранено отдельно. '
           'Разрыв только на пробеле не называется утечкой имени. Выборочные фильтры требуют отдельной проверки.',
           'Частичная разметка не превращает остальной текст в доказанно безопасный. Ложные блокировки всего запроса '
           'считаются только на полностью размеченных отрицательных примерах; для PII-Bench отдельно показаны маски '
           'в пределах классов его разметки. Отсутствующие классы имеют «—», а не выдуманный ноль.','',
           '## Основной корпус: полный Analyzer','']
    lines+=class_table('ner_migration',kinds=TARGET_ENTITY_TYPES)
    lines+=['','## Основной корпус: отдельный NER','']+class_table('ner_migration','ner',TARGET_ENTITY_TYPES)
    lines+=['','## Все классы: контекстные контроли','',
            '52 прежних положительных/отрицательных случая. Для многих классов здесь всего один положительный пример: '
            'это проверка сохранения поведения, не оценка полноты класса на произвольных данных.','']
    lines+=class_table('all_class_context',kinds=SUPPORTED_ENTITY_TYPES)
    lines+=['','## PII-Bench','',
            '[Источник](https://huggingface.co/datasets/hivetrace/pii-bench), Apache-2.0, ревизия '
            '`cd6a18ace16daf23e79247ccf1e2b245d4054654`; SHA обоих Parquet и все координаты проверены. '
            'Автор модели уже оценивал этот набор: это первый локальный прогон, но не независимый от выбора модели контроль. '
            'Тождество авторскому validation из 8224 строк не установлено.',
            '`NAME → PERSON`, `ADDRESS → RU_ADDRESS` (весь адрес), `TOKEN → AUTH_TOKEN` для строгой классификации. '
            'Маски LOCATION и других секретных классов могут защищать значение, не совпадая с этими метками. '
            'CVC вне согласованного объёма; результаты на нём не являются обещанием поддержки. Разметка не менялась.','',
            '### Entity: 910 примеров','']
    lines+=class_table('pii_entity')
    lines+=['','### Domain: 900 примеров','']+class_table('pii_domain')
    lines+=['','## Корпуса и лишнее маскирование','',
            '| Корпус | Примеров | Значений | Защищено BERT | Защищено Tiny2 | Лишних масок BERT/Tiny2¹ |',
            '|---|---:|---:|---:|---:|---:|']
    for group,rows in result['modes']['analyzer']['groups'].items():
        a,b=(rows[system]['counts'] for system in ('reference','candidate'))
        lines.append(f"| {group} | {a['cases']} | {a['gold_values_all_requests']} | {covered(a)} | {covered(b)} | "
                     f"{a['false_positives']}/{b['false_positives']} |")
    lines+=['','¹ Точные несовпадения в пределах разметки: сюда попадают и неверные границы защищённого значения, '
            'поэтому это не число утечек. Для ДКБ точные типы в этой таблице используют первую каноническую метку; '
            'сквозная проверка принимает исходные альтернативные типы из манифеста без изменения разметки.','']
    for group in ('instruction_regressions','pii_domain'):
        a,b=(result['modes']['analyzer']['groups'][group][s]['counts'] for s in ('reference','candidate'))
        lines.append(f"- `{group}`: отрицательных примеров {a.get('negative_cases',0)}; с масками классов разметки "
                     f"{a.get('negative_cases_with_in_scope_mask',0)} → {b.get('negative_cases_with_in_scope_mask',0)}; "
                     f"с любыми масками {a.get('negative_cases_with_any_mask',0)} → {b.get('negative_cases_with_any_mask',0)}. "
                     f"Лишних скрытых символов относительно разметки этих классов "
                     f"{a.get('in_scope_extra_masked_characters',0)} → {b.get('in_scope_extra_masked_characters',0)}.")
    lines+=['','## Расхождения по примерам','']
    changes={'improvements':[],'regressions':[]}
    for group,rows in result['modes']['analyzer']['groups'].items():
        old={r['case_id']:r for r in rows['reference']['cases']}
        for new in rows['candidate']['cases']:
            a,b=old[new['case_id']],new
            left=sum(v['significant_fully_covered'] for v in a['coverage']['values'])
            right=sum(v['significant_fully_covered'] for v in b['coverage']['values'])
            key='improvements' if right>left else 'regressions' if right<left else None
            if key:
                lost=[v['entity_type'] for before,v in zip(a['coverage']['values'],b['coverage']['values'])
                          if before['significant_fully_covered'] and not v['significant_fully_covered']]
                changes[key].append({'case_id':new['case_id'],'protected_before':left,'protected_after':right,
                                     'lost_entity_types':lost})
    result['coverage_changes']=changes
    in_scope_losses=[row for row in changes['regressions'] if any(k!='CVC_OUT_OF_SCOPE' for k in row['lost_entity_types'])]
    lines+=['Снижение покрытия: '+str(len(in_scope_losses))+' случаев в согласованных классах; ещё '
            +str(len(changes['regressions'])-len(in_scope_losses))+' относятся только к исключённому CVC. '
            'Эти различия не приняты за допустимые и не исправлялись подбором исключений.','']
    for label,key in [('Улучшения покрытия','improvements'),('Ухудшения покрытия','regressions')]:
        lines.append(f"**{label}: {len(changes[key])} примеров.**")
        examples=(in_scope_losses if key=='regressions' else
                  [c for c in changes[key] if c['case_id'].startswith(('pii_','person_','name_'))])[:5]
        for row in examples:
            item=by_id[row['case_id']]
            text=item.case.text.replace('\n',' ')[:180].replace('|','/')
            lines.append(f"- `{row['case_id']}`: «{text}» — защищено значений {row['protected_before']} → {row['protected_after']}.")
        lines.append('')
    lines+=['## Решение и ограничения','',
            'Q01/Q02/Q03 остаются открытыми качественными расхождениями: `Don` в `Done`, ложные классы на названиях '
            'сущностей/пунктуации, разделение `АННА СОКОЛОВА` между ORGANIZATION и PERSON. '
            'Строгие проверки не ослабляются; прежние оговорки BERT не приняты автоматически для Tiny2.',
            'Результаты сквозных проверок, фильтров и расширения JSON-значений приведены в дополнении ниже. '
            'Ошибки подготовки одноразовой обвязки (ожидание маршрута Nginx) не скрыты и не зачтены как успешные проверки продукта.',
            'Платформенный код, веса и рабочая калибровка в Э9 не менялись. По таблицам нельзя автоматически принять переход '
            'или обещать ускорение/ёмкость для 400 пользователей: окончательное решение владельца — после Э10.','']

    flows={}
    for policy in ('content','logistic'):
        path=OUT/f'flow-{policy}.json'
        if path.is_file():
            flows[policy]=json.loads(path.read_text())
    result['protocol_and_egress']=flows
    if flows:
        lines+=['## Сквозная проверка','',
                'Настоящие Analyzer/Triton, штатный LiteLLM и Redis; внешний провайдер заменён '
                'управляемым HTTP/SSE-сервером с фиксацией фактически полученного тела. '
                'Реальные LLM, Codex/OpenCode и корпоративные средства безопасности в этом этапе не использовались.','']
        for policy,data in flows.items():
            lines.append(f"- `{policy}`: протокольный набор {data['checks']['protocol']['status']}, "
                         f"ДКБ/границы/Base64 {data['checks']['dkb']['status']}, очистка {data['cleanup_ok']}. "
                         'По 8 текстовых/инструментальных сочетаний, 8 блокировок без провайдера, '
                         '12 ходов ветвящихся диалогов под одним ключом, 2 проверки encrypted_content.')
        data=flows.get('content',{})
        diagnostics=data.get('diagnostics',[])
        if diagnostics:
            result['status']='comparison_complete_security_regressions_require_owner_decision'
            lines+=['','### Подтверждённые пропуски','',
                    '**Приёмка безопасности не пройдена.** На 7 случаях выполнено 56 запросов: '
                    '28 MASK и 28 BLOCK, оба API и оба режима потока. В 28 MASK-запросах '
                    'провайдер получил исходные фрагменты. В BLOCK 16 запросов остановлены из-за других '
                    'найденных сущностей, но 12 прошли провайдеру: полностью пропущенные email, '
                    'неполный номер паспорта и короткий пароль. Это не ошибки локального объёма памяти.','',
                    '| Случай | Что осталось открытым | Чем прикрывал BERT |',
                    '|---|---|---|',
                    '| `pii_domain__del_013` | `подъезд 2` в адресе | PASSWORD, неверный класс |',
                    '| `pii_domain__del_036` | `набережная 24` после Фрунзенская | LOCATION на всём адресе |',
                    '| `pii_domain__hr_084` | `москва, проспект вернадского 30` | LOCATION/LOGIN/PASSWORD, неверные классы |',
                    '| `pii_domain__ss_009` | Кириллический email | LOGIN, неверный класс |',
                    '| `pii_domain__ss_026` | `Михаил Соколов` перед email | PERSON/LOGIN, смешанные классы |',
                    '| `pii_entity__passport_058` | Номер `122648` без серии, в паспортном контексте | PASSWORD, неверный класс |',
                    '| `tiny2_component_study__ctrl_labeled_prompt` | `!?$` после «Пароль:» | PASSWORD |','',
                    '«Защищено BERT» не означает правильную классификацию: часть защиты была случайным '
                    'лишним срабатыванием на другой класс. Устранение такого срабатывания всё равно может '
                    'снизить защиту от передачи исходного значения. Ни прежняя ошибка, ни новый пропуск '
                    'не используются для изменения разметки.','',
                    '### Выборочные фильтры','',
                    'На точном тексте «Клиент АННА СОКОЛОВА подтвердил заявку.» эталон возвращает PERSON '
                    'на всё имя. Tiny2 возвращает ORGANIZATION на «АННА» и PERSON на «СОКОЛОВА». '
                    'Все классы защищают 12/12 непробельных символов у обеих версий; фильтр PERSON '
                    'даёт 12/12 у BERT и 8/12 у Tiny2. Это дополнительная регрессия фильтра, '
                    'не видимая по покрытию любыми масками. Проверены 40 сочетаний текста/фильтра/версии.',
                    'Q01 (Don в Done), Q02 (PE/ORGANIZA и пунктуация) также воспроизведены. '
                    'Это не разрешение вводить исключения для отдельных слов.','',
                    '### Расширение JSON-значений','',
                    'В JSON 2.txt масками закрыто 158 → 170 символов из 906; в JSON 5.txt — '
                    '1781 → 1785 из 1799. Дополнительные интервалы остаются внутри строкового значения. '
                    'Границы структуры не маскируются, исходные значения восстанавливаются в сквозных '
                    'проверках ДКБ. Эти данные не доказывают отсутствия избыточных масок на произвольном JSON.','',
                    '## Предлагаемый отдельный план исправлений','',
                    'Пока не согласован и не реализован; пороги приёмки не назначаются.',
                    '1. Зафиксировать новые пропуски и фильтры как строгие регрессии. Согласовать, '
                    'входит ли в рабочий контракт Unicode-email и неполный паспортный номер в явном контексте.',
                    '2. Для коротких паролей исследовать общую грамматику явно заданного значения, '
                    'включая пунктуационные пароли и отрицательные случаи. Не делать исключение только для !?$.',
                    '3. Улучшить разбор адресов едиными правилами: положение типа улицы, регистр, дом '
                    'и подъезд. Проверить соседние числа и обычный текст; не добавлять три конкретных адреса в словарь.',
                    '4. По согласованному объёму проверить общее распознавание Unicode-email и '
                    'паспортного номера без серии с сильным контекстом, без превращения любого числа в документ.',
                    '5. Имена, смешение PERSON/ORGANIZATION и Q01/Q02 разобрать отдельным модельным '
                    'экспериментом с независимыми контролями. Не возвращать BERT, не включать второй движок '
                    'и не менять облегчённый spaCy/калибровку без нового согласования. '
                    'Регрессии относительно main не считаются автоматически отложенной задачей #105.',
                    '6. Повторить затронутые корпуса и сквозную диагностику в MASK/BLOCK, '
                    'пересчитать полную таблицу и представить её владельцу. Не объявлять потери допустимыми за него.','']
    for name in ('topologies-initial','topologies-repeat','validation'):
        path=OUT/(name+'.json')
        if path.is_file():
            result[name]=json.loads(path.read_text())
    if 'topologies-repeat' in result:
        data=result['topologies-repeat']
        lines+=['## Технические проверки','',
                f"Повтор развёртывания: {data.get('status')}, {len(data.get('cases',[]))}/10 сценариев. "
                'Первый прогон остановился после 7/10 без OOM/рестартов; он сохранён как неуспешный. '
                'Причина первого сбоя не установлена. В тест добавлено место исключения; '
                'успешный повтор не считается исправлением этого риска.','']
    if 'validation' in result:
        validation=result['validation']
        lines+=['Регрессионные проверки:', '']
        for check in validation.get('checks',[]):
            lines.append(f"- `{check['command']}`: {check['status']}. {check['summary']}")
        lines+=['', 'Числа повторных наборов означают выполнения тестов, а не уникальные случаи. '
                'Эти успешные проверки не отменяют перечисленные пропуски маскирования. '
                'GitHub CI для незакоммиченного состояния не запускался.','']
        if validation.get('local_stand',{}).get('status')=='restored':
            lines+=['Локальный стенд восстановлен на последнем `main@1c8c78d`: 7 healthy, '
                    'те же контейнеры/тома, шесть защищённых файлов без изменений. '
                    'Проверены 30 файлов Analyzer, 9 guardrail и 6 файлов модели; пять API-проб прошли. '
                    'Временных контейнеров, сетей и помеченных тестовых томов не осталось. '
                    'Неизвестные анонимные тома не удалялись; общей очистки Docker не было.','']
    destination=ROOT/'presidio/evaluation/reports/tiny2-quality-comparison.json'
    details=destination.with_name('tiny2-quality-comparison.cases.json.gz')
    details.write_bytes(gzip.compress(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode(),mtime=0))
    summary=dict(result)
    summary['details_artifact']={'file':details.name,'sha256':hashlib.sha256(details.read_bytes()).hexdigest(),
                                 'bytes':details.stat().st_size,'contains_raw_text':False}
    summary['modes']={}
    for mode,study in result['modes'].items():
        compact={'metadata':study['metadata'],'groups':{}}
        for name,group in study['groups'].items():
            compact['groups'][name]={system:{k:v for k,v in data.items() if k!='cases'} for system,data in group.items()}
        summary['modes'][mode]=compact
    destination.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    (ROOT/'docs/research/tiny2-quality-comparison.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'case_count':len(cases),'request_count':len(cases)*4,
                      'improvements':len(changes['improvements']),'regressions':len(changes['regressions']),
                      'report_bytes':destination.stat().st_size}))


if __name__ == "__main__":
    main()
