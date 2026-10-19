/* Проверяем границу отображения: диагностика остаётся данными, а не текстом страницы. */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ProductError, errorText, responseError, userMessage, userReason, userWarnings } from '../app/web/static/presentation.mjs';

const diagnostics = [
  'Используется ограниченный локальный разбор без внешней модели.',
  'Внешняя модель недоступна или её ответ не прошёл проверку. Используется ограниченный локальный разбор.',
  'Использовано объяснение по данным каталога без помощи внешней модели.',
  'SQLite contains 4 records; extraction_source=fallback; provider unavailable',
];
const forbidden = /SQLite|LLM|fallback|provider|extraction_source|локальн|парсер|внешн.{0,15}модел|синхронизац/iu;

for (const status of ['ok', 'no_candidates', 'uninterpretable', 'empty_catalog', 'неизвестное состояние']) {
  test(`Состояние «${status}» не выводит исходное диагностическое сообщение`, () => {
    const response = { status, message: diagnostics.join(' '), warnings: diagnostics, extraction_source: 'fallback', budget_status: 'not_requested' };
    const original = structuredClone(response);
    assert.doesNotMatch(userMessage(response), forbidden);
    assert.match(userMessage(response), /[а-яё]/iu);
    assert.deepEqual(userWarnings(response), []);
    assert.deepEqual(response, original);
    if (status !== 'ok') assert.doesNotMatch(userMessage(response), /Подобрали вина/u);
  });
}

for (const [axis, question] of [
  ['color', 'Какое вино предпочитаете: красное, белое или розовое?'],
  ['sweetness', 'Предпочитаете сухое, полусухое или сладкое вино?'],
  ['food', 'К какому блюду подбираете вино?'],
]) {
  test(`Уточнение «${axis}» соответствует проверенному намерению`, () => {
    const text = userMessage({ status: 'clarification', message: diagnostics[0], intent: { missing_information: [axis] } });
    assert.equal(text, question);
    assert.equal(text.split('?').length - 1, 1);
  });
}

test('Недоступность модели не мешает успешно разобранному запросу', () => {
  for (const source of ['llm', 'fallback']) {
    const response = { status: 'ok', extraction_source: source, warnings: diagnostics };
    assert.equal(userMessage(response), 'Подобрали вина по вашим пожеланиям.');
    assert.deepEqual(userWarnings(response), []);
    assert.equal(response.extraction_source, source);
    assert.deepEqual(response.warnings, diagnostics);
  }
});

test('Недоступная проверка цены остаётся заметной без описания механики', () => {
  const warnings = userWarnings({ budget_status: 'budget_constraint_unavailable', warnings: diagnostics });
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /не можем гарантировать соблюдение бюджета/u);
  assert.doesNotMatch(warnings[0], forbidden);
  assert.deepEqual(userWarnings({ budget_status: 'applied', warnings: diagnostics }), []);
});

test('Недостаток сведений о блюде и вкусе не скрывается вместе с диагностикой', () => {
  const response = { warnings: [
    ...diagnostics,
    'Не все гастрономические пожелания удалось сопоставить с реальными категориями каталога; неподтверждённые категории не учитывались.',
    'В описаниях выбранных вин не найдено подтверждений указанным вкусовым пожеланиям.',
  ] };
  const warnings = userWarnings(response);
  assert.equal(warnings.length, 2);
  assert.match(warnings[0], /блюдом удалось учесть не полностью/u);
  assert.match(warnings[1], /недостаточно сведений/u);
  warnings.forEach((text) => assert.doesNotMatch(text, forbidden));
});

test('Объяснение сохраняет неопределённость и не показывает численные детали ранжирования', () => {
  const uncertain = 'Каталог не даёт достаточных сведений, чтобы подтвердить отсутствие нежелательного свойства «грубая терпкость».';
  assert.equal(userReason({ key: 'negative_taste:0', reason: uncertain }), uncertain);
  assert.equal(userReason({ key: 'negative_taste:0', reason: 'В описании указаны жёсткие танины. Балл снижен.' }), 'В описании указаны жёсткие танины.');
  for (const score of ['+6', '-4', '+2.25', '+0.00']) {
    assert.equal(userReason({ key: 'personalization', reason: `История ваших оценок: вам понравилось это вино. Влияние на балл: ${score}.` }), 'История ваших оценок: вам понравилось это вино.');
  }
  assert.equal(userReason({ key: 'catalog', reason: diagnostics[0] }), null);
  assert.equal(userReason({ key: 'debug', reason: diagnostics[1] }), null);
});

test('Цитата источника не заменяется новой дегустационной характеристикой', () => {
  const reason = 'К пожеланию «насыщенное» найдено упоминание в описании: «Вкус насыщенный». Вам понравилось похожее вино.';
  assert.equal(userReason({ key: 'taste:0', reason }), reason);
});

for (const status of [404, 422, 429, 500, 503]) {
  test(`Ошибка ${status} не передаёт пользователю технические подробности`, () => {
    const text = errorText(responseError(status));
    assert.match(text, /[а-яё]/iu);
    assert.doesNotMatch(text, forbidden);
    assert.doesNotMatch(text, /HTTP|traceback|API/iu);
  });
}

test('Произвольная ошибка и неизвестный код не показывают сырой текст', () => {
  const raw = new Error('SQLite traceback; Authorization: секрет; provider failed');
  assert.equal(errorText(raw), 'Не удалось выполнить действие. Попробуйте ещё раз.');
  assert.equal(errorText(new ProductError(raw.message)), 'Не удалось выполнить действие. Попробуйте ещё раз.');
  assert.match(errorText(new ProductError('network')), /Не удалось связаться с сервисом/u);
  assert.match(errorText(new ProductError('timeout')), /слишком много времени/u);
});
