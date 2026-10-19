/* Отделяем полезные пользователю сообщения от диагностических данных ответа. */
export function userMessage(response) {
  const messages = {
    ok: 'Подобрали вина по вашим пожеланиям.',
    no_candidates: 'Не нашли вина по вашим пожеланиям. Попробуйте изменить одно из условий.',
    uninterpretable: 'Не удалось точно понять все пожелания. Попробуйте описать их немного иначе.',
    empty_catalog: 'Сейчас нет доступных вин для подбора. Попробуйте позже.',
  };
  if (response.status === 'clarification') {
    const questions = {
      color: 'Какое вино предпочитаете: красное, белое или розовое?',
      sweetness: 'Предпочитаете сухое, полусухое или сладкое вино?',
      food: 'К какому блюду подбираете вино?',
    };
    const missing = response.intent?.missing_information || [];
    return questions[missing[0]] || 'Уточните, пожалуйста, ваши пожелания к вину.';
  }
  return messages[response.status] || 'Не удалось завершить подбор. Попробуйте ещё раз.';
}

export function userWarnings(response) {
  const result = [];
  if (response.budget_status === 'budget_constraint_unavailable') {
    result.push('Цены не указаны, поэтому мы не можем гарантировать соблюдение бюджета.');
  }
  // Неизвестные предупреждения не выводятся автоматически: для них нужен пользовательский текст.
  const notices = [
    ['Не все гастрономические пожелания удалось сопоставить', 'Сочетание с блюдом удалось учесть не полностью. Можно уточнить блюдо.'],
    ['В описаниях выбранных вин не найдено подтверждений', 'В описаниях этих вин недостаточно сведений, чтобы подтвердить ваши пожелания к вкусу.'],
  ];
  for (const [prefix, text] of notices) {
    if ((response.warnings || []).some((warning) => typeof warning === 'string' && warning.startsWith(prefix))) result.push(text);
  }
  return result;
}

export function userReason(part) {
  if (typeof part.reason !== 'string') return null;
  if (part.key === 'constraints') return 'Соответствует указанным вами условиям выбора.';
  if (part.key === 'personalization') {
    return part.reason.replace(/\s*Влияние на балл:\s*[+-]?\d+(?:[.,]\d+)?\.\s*$/u, '').trim();
  }
  if (part.key.startsWith('negative_taste:')) {
    return part.reason.replace(/\s*Балл снижен\.\s*$/u, '').trim();
  }
  if (/^(food:|taste:|soft:)/u.test(part.key) || part.key === 'rating') return part.reason;
  return null;
}

const errorMessages = {
  network: 'Не удалось связаться с сервисом. Попробуйте ещё раз.',
  timeout: 'Ответ занял слишком много времени. Попробуйте ещё раз.',
  invalid_request: 'Проверьте пожелания и попробуйте ещё раз.',
  not_found: 'Запрошенная страница или вино не найдены.',
  too_many_requests: 'Слишком много запросов. Подождите немного и попробуйте ещё раз.',
  general: 'Не удалось выполнить действие. Попробуйте ещё раз.',
};

export class ProductError extends Error {
  constructor(kind = 'general') {
    super(errorMessages[kind] || errorMessages.general);
  }
}

export function responseError(status) {
  return new ProductError({ 404: 'not_found', 422: 'invalid_request', 429: 'too_many_requests' }[status]);
}

export function errorText(error) {
  return error instanceof ProductError ? error.message : errorMessages.general;
}
