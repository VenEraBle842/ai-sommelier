/* Факты берём из карточек, а сообщения для пользователя — из отдельного словаря. */
import { ProductError, errorText, responseError, userMessage, userReason, userWarnings } from './presentation.mjs';
'use strict';
const byId = (id) => document.getElementById(id);
const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
};
const state = { userId: crypto.randomUUID(), intent: null, busy: false, feedback: new Map(), view: 'initial' };
try {
  const saved = localStorage.getItem('svoe-vino-user');
  if (saved && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(saved)) state.userId = saved;
  localStorage.setItem('svoe-vino-user', state.userId);
} catch {
  byId('identity-note').textContent = 'Не удалось запомнить вас в этом браузере. После закрытия вкладки история оценок может быть недоступна.';
}

async function api(path, payload) {
  let response;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 180000);
  try {
    response = await fetch(path, {
      method: payload === undefined ? 'GET' : 'POST',
      headers: payload === undefined ? {} : { 'Content-Type': 'application/json' },
      body: payload === undefined ? undefined : JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!response.ok) throw responseError(response.status);
    let data;
    try { data = await response.json(); }
    catch { throw new ProductError(); }
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new ProductError('timeout');
    if (error instanceof ProductError) throw error;
    throw new ProductError('network');
  } finally {
    clearTimeout(timer);
  }
}

function sourceLink(url, text) {
  const link = element('a', '', text);
  try {
    const parsed = new URL(url);
    if (parsed.protocol === 'https:' && parsed.hostname === 'vino-svoe.ru') link.href = parsed.href;
  } catch { /* Неверная ссылка остаётся текстом и не исполняется. */ }
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  return link;
}

function conversation(title, text) {
  const section = element('article');
  section.append(element('h3', '', title), element('p', '', text));
  const transcript = byId('conversation');
  transcript.append(section);
  while (transcript.children.length > 3) transcript.firstElementChild.remove();
  transcript.scrollTop = transcript.scrollHeight;
}

function setBusy(busy) {
  state.busy = busy;
  byId('submit').disabled = busy;
  byId('reset').disabled = busy;
  byId('message').disabled = busy;
  document.querySelectorAll('[data-example]').forEach((button) => { button.disabled = busy; });
  byId('submit').textContent = busy ? 'Подбираем вино…' : 'Подобрать вино';
  byId('results').setAttribute('aria-busy', String(busy));
}

const number = (value) => new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(Number(value));

function visibleReasons(item, intent) {
  const required = item.breakdown.filter((part) => part.key === 'personalization' || part.key.startsWith('negative_taste:'));
  const query = item.breakdown.filter((part) => /^(food:|taste:|soft:|constraints)/.test(part.key)).flatMap((part) => {
    if (part.key !== 'constraints') return [part];
    const actual = item.wine;
    const hard = intent.hard_constraints;
    const facts = [
      [hard.color, 'Цвет соответствует запросу', actual.color],
      [hard.sweetness, 'Сладость соответствует запросу', actual.sweetness],
      [hard.grapes.length, 'Сорта из вашего запроса есть в составе', actual.grapes.join(', ')],
      [hard.regions.length, 'Регион соответствует запросу', actual.region],
      [hard.producer, 'Производитель соответствует запросу', actual.producer],
      [hard.source_categories.length, 'Категория соответствует запросу', actual.source_category],
    ].filter(([requested, label, value]) => requested && value);
    return facts.length ? facts.map(([requested, label, value]) => ({ ...part, reason: `${label}: ${value}.` })) : [part];
  });
  return [...query.slice(0, 2), ...required].slice(0, 4);
}

function renderWine(item, intent) {
  const wine = item.wine;
  const isRecommendation = Array.isArray(item.breakdown);
  const row = element('article', 'wine-card');
  const visual = element('div', 'wine-visual');
    const placeholder = element('span', 'image-unavailable', 'Изображение не предоставлено');
  visual.append(placeholder);
  if (wine.rating !== null) visual.append(element('span', 'catalog-rating', `Рейтинг ${number(wine.rating)}`));
  if (wine.image_url) {
    try {
      const url = new URL(wine.image_url);
      if (url.protocol === 'https:') {
        placeholder.textContent = 'Загружаем изображение…';
        const image = element('img');
        image.src = url.href;
        image.alt = wine.name;
        image.loading = 'lazy';
        image.referrerPolicy = 'no-referrer';
        image.addEventListener('load', () => { placeholder.hidden = true; });
        image.addEventListener('error', () => {
          image.remove();
          placeholder.textContent = 'Изображение не предоставлено';
          placeholder.hidden = false;
        });
        visual.append(image);
      }
    } catch { /* Отсутствие изображения не заменяется выдуманной бутылкой. */ }
  }
  row.append(visual);
  const content = element('div', 'wine-content');
  content.append(element('p', 'wine-category', wine.source_category || [wine.color, wine.sweetness].filter(Boolean).join(' · ') || 'Вино из каталога'));
  const title = element('h3');
  const titleButton = element('button', 'wine-title', wine.name);
  titleButton.type = 'button';
  titleButton.setAttribute('aria-haspopup', 'dialog');
  titleButton.addEventListener('click', () => openDetails(wine.id));
  title.append(titleButton);
  content.append(title);
  if (wine.producer) content.append(element('p', 'producer', wine.producer));
  const facts = element('dl', 'wine-facts');
  for (const [label, value] of [['Регион', wine.region], ['Виноград', wine.grapes.join(', ')]]) {
    if (value) facts.append(element('dt', '', label), element('dd', '', value));
  }
  content.append(facts);
  if (wine.food_pairings.length) {
    const pairings = element('p', 'pairings');
    pairings.append(element('span', '', 'К блюдам: '), document.createTextNode(wine.food_pairings.join(', ')));
    content.append(pairings);
  }
  content.append(element('p', wine.price_rub === null ? 'price' : 'price known', wine.price_rub === null ? 'Цена не указана' : `${number(wine.price_rub)} ₽`));
  if (isRecommendation) {
    const why = element('section', 'why');
    why.append(element('h4', '', 'Почему подходит'));
    const reasons = element('ul');
    visibleReasons(item, intent).forEach((part) => {
      const fullText = part.key === 'constraints' ? part.reason : userReason(part);
      if (!fullText) return;
      const fragments = fullText.split(';');
      const text = part.key === 'personalization' && fragments.length > 2 ? `${fragments.slice(0, 2).join(';')}. Подробнее — в объяснении выбора.` : fullText;
      reasons.append(element('li', '', text));
    });
    why.append(reasons);
    content.append(why);
    const details = element('details', 'score');
    details.append(element('summary', '', 'Подробнее о выборе'));
    for (const text of [...new Set(item.breakdown.map(userReason).filter(Boolean))]) {
      details.append(element('p', '', text));
    }
    details.append(element('p', '', 'Ваши оценки помогают выбрать среди подходящих вин. Указанные вами условия сохраняются.'));
    content.append(details);
  }
  const actions = element('div', 'wine-actions');
  const original = sourceLink(wine.source_url, 'На сайте «Своё Вино»');
  original.addEventListener('click', () => { api('/feedback', { user_id: state.userId, wine_id: wine.id, event_type: 'opened' }).catch(() => {}); });
  const more = element('button', 'text-button', 'Карточка вина');
  more.type = 'button';
  more.addEventListener('click', () => openDetails(wine.id));
  actions.append(original, more);
  content.append(actions);
  if (isRecommendation) {
    const feedback = element('div', 'feedback');
    const confirmation = element('p', 'small-note');
    confirmation.setAttribute('role', 'status');
    for (const [kind, label] of [['like', 'Нравится +'], ['dislike', 'Не моё −']]) {
      const button = element('button', '', label);
      button.type = 'button';
      button.setAttribute('aria-label', `${label}: ${wine.name}`);
      button.setAttribute('aria-pressed', String(state.feedback.get(wine.id) === kind));
      button.dataset.feedback = kind;
      button.addEventListener('click', async () => {
        const buttons = feedback.querySelectorAll('button');
        buttons.forEach((node) => { node.disabled = true; });
        confirmation.textContent = 'Сохраняем оценку…';
        try {
          await api('/feedback', { user_id: state.userId, wine_id: wine.id, event_type: kind });
          state.feedback.set(wine.id, kind);
          buttons.forEach((node) => node.setAttribute('aria-pressed', String(node.dataset.feedback === kind)));
          confirmation.textContent = 'Оценка сохранена. Учтём её в следующем подборе.';
        } catch (error) { confirmation.textContent = errorText(error); }
        finally { buttons.forEach((node) => { node.disabled = false; }); }
      });
      feedback.append(button);
    }
    feedback.append(confirmation);
    content.append(feedback);
  }
  row.append(content);
  return row;
}

function showState(title, text, { error = false, actionText, action } = {}) {
  const panel = element('div', error ? 'state-panel error' : 'state-panel');
  panel.append(element('h3', '', title), element('p', '', text));
  if (actionText && action) {
    const button = element('button', 'utility-button', actionText);
    button.type = 'button';
    button.addEventListener('click', action);
    panel.append(button);
  }
  byId('results').replaceChildren(panel);
}

async function showInitialCatalog() {
  state.view = 'initial';
  byId('selection-heading').textContent = 'Каталог для подбора';
  byId('result-message').textContent = 'Показаны карточки каталога, а не рекомендации. Отправьте запрос, чтобы подобрать подходящие вина.';
  byId('result-count').hidden = true;
  byId('warnings').replaceChildren();
  byId('results').setAttribute('aria-busy', 'true');
  showState('Загружаем карточки…', 'Подождите немного.');
  try {
    const wines = await api('/wines?limit=3');
    if (state.view !== 'initial') return;
    if (wines.length) {
      byId('results').replaceChildren(...wines.map((wine) => renderWine({ wine }, null)));
    } else {
      byId('selection-heading').textContent = 'Пока нет доступных вин';
      byId('result-message').textContent = 'Сейчас подбор недоступен.';
      showState('Попробуйте позже', 'Пока не можем предложить вина для подбора.');
    }
  } catch (error) {
    if (state.view !== 'initial') return;
    byId('result-message').textContent = 'Не удалось загрузить карточки каталога.';
    showState('Каталог недоступен', errorText(error), { error: true, actionText: 'Повторить загрузку', action: showInitialCatalog });
  } finally {
    if (state.view === 'initial') byId('results').setAttribute('aria-busy', 'false');
  }
}

byId('request-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const message = byId('message').value.trim();
  if (state.busy) return;
  if (!message) {
    byId('result-message').textContent = 'Опишите пожелания к вину перед отправкой.';
    byId('message').focus();
    return;
  }
  state.view = 'loading';
  setBusy(true);
  byId('selection-heading').textContent = 'Подбираем вино';
  byId('result-count').hidden = true;
  showState('Сверяем пожелания с каталогом', 'Учитываем ваш запрос и сохранённые оценки.');
  const loading = element('div', 'loading-line');
  loading.setAttribute('aria-hidden', 'true');
  byId('results').before(loading);
  byId('result-message').textContent = 'Подбираем вина по вашим пожеланиям…';
  byId('warnings').replaceChildren();
  try {
    const response = await api('/chat', { message, user_id: state.userId, previous_intent: state.intent, limit: 3 });
    state.view = response.status;
    if (state.intent === null) byId('conversation').replaceChildren();
    conversation('Ваш запрос', message);
    state.intent = ['clarification', 'uninterpretable', 'no_candidates'].includes(response.status) ? response.intent : null;
    byId('examples').hidden = true;
    byId('reset').hidden = false;
    byId('message').value = '';
    const displayMessage = userMessage(response);
    byId('result-message').textContent = displayMessage;
    userWarnings(response).forEach((warning) => byId('warnings').append(element('p', 'notice', warning)));
    byId('results').replaceChildren();
    if (response.status === 'ok') {
      byId('selection-heading').textContent = 'Ваша подборка';
      byId('message-label').textContent = 'Что подберём дальше?';
      byId('result-count').textContent = `Подходящих вин: ${response.recommendations.length}`;
      byId('result-count').hidden = false;
      response.recommendations.forEach((wine) => byId('results').append(renderWine(wine, response.intent)));
    } else {
      const titles = { clarification: 'Уточнение запроса', no_candidates: 'Подходящих вин не найдено', empty_catalog: 'Пока нет доступных вин', uninterpretable: 'Нужно уточнить пожелания' };
      byId('selection-heading').textContent = titles[response.status] || 'Продолжение подбора';
      if (response.status === 'clarification') conversation('Вопрос помощника', displayMessage);
      byId('message-label').textContent = response.status === 'clarification' ? 'Ваш ответ' : 'Уточните пожелания';
      const texts = {
        clarification: ['Ответьте на вопрос', 'Введите ответ в поле запроса. Предыдущие пожелания сохранены.'],
        no_candidates: ['Попробуйте изменить условия', 'Уточните цвет, сладость, блюдо или сорт. Кнопка «Сбросить» начнёт новый подбор, сохранив ваши оценки.'],
        empty_catalog: ['Попробуйте позже', 'Сейчас не можем предложить вина для подбора.'],
        uninterpretable: ['Опишите запрос иначе', 'Например: «красное сухое к стейку». Можно указать блюдо, цвет, сладость, сорт или регион.'],
      };
      const [title, text] = texts[response.status] || ['Уточните запрос', 'Введите пожелания в поле запроса.'];
      showState(title, text);
    }
  } catch (error) {
    state.view = 'error';
    byId('selection-heading').textContent = 'Не удалось выполнить подбор';
    byId('result-message').textContent = errorText(error);
    showState('Запрос не завершён', 'Текст сохранён в поле запроса. Его можно отправить повторно.', { error: true, actionText: 'Повторить запрос', action: () => byId('request-form').requestSubmit() });
  } finally {
    loading.remove();
    setBusy(false);
    if (state.intent) byId('message').focus();
  }
});

byId('reset').addEventListener('click', () => {
  state.intent = null;
  byId('conversation').replaceChildren();
  byId('message-label').textContent = 'Что подберём сегодня?';
  byId('message').value = '';
  byId('message').focus();
  byId('reset').hidden = true;
  byId('examples').hidden = false;
  showInitialCatalog();
});
document.querySelectorAll('[data-example]').forEach((button) => button.addEventListener('click', () => {
  byId('message').value = button.dataset.example;
  byId('message').focus();
}));
document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', () => byId(button.dataset.close).close()));

async function loadHistory() {
  const events = await api(`/users/${state.userId}/history?limit=200`);
  state.feedback.clear();
  for (const event of [...events].reverse()) {
    if (event.event_type === 'like' || event.event_type === 'dislike') state.feedback.set(event.wine_id, event.event_type);
  }
  return events;
}
byId('history-open').addEventListener('click', async () => {
  const dialog = byId('history-dialog');
  if (!dialog.open) dialog.showModal();
  const content = byId('history-content');
  content.textContent = 'Читаем историю…';
  try {
    const events = await loadHistory();
    content.replaceChildren();
    if (!events.length) content.append(element('p', '', 'Здесь появятся ваши подборки и оценки. Отметьте понравившееся вино, чтобы следующий выбор стал ближе к вашему вкусу.'));
    const names = { shown: 'Показано в подборке', like: 'Вам понравилось', dislike: 'Вам не подошло', opened: 'Открыт источник' };
    for (const event of events) {
      const row = element('article', 'history-item');
      const time = element('time', '', new Date(event.timestamp).toLocaleString('ru-RU'));
      time.dateTime = event.timestamp;
      row.append(element('p', 'history-kind', names[event.event_type]), sourceLink(event.wine.source_url, event.wine.name + ' ↗'), element('br'), time);
      content.append(row);
    }
  } catch (error) { content.textContent = errorText(error); }
});

async function openDetails(id) {
  const dialog = byId('wine-dialog');
  const content = byId('wine-details');
  const loadingTitle = element('h2', '', 'Читаем карточку…');
  loadingTitle.id = 'wine-heading';
  content.replaceChildren(loadingTitle);
  if (!dialog.open) dialog.showModal();
  try {
    const wine = await api(`/wines/${id}`);
    const title = element('h2', '', wine.name);
    title.id = 'wine-heading';
    content.replaceChildren(title);
    const facts = element('dl');
    for (const [label, value] of [['Производитель', wine.producer], ['Регион', wine.region], ['Виноград', wine.grapes.join(', ')], ['Категория', wine.source_category], ['К столу', wine.food_pairings.join(', ')], ['Температура подачи', wine.serving_temperature], ['Крепость', wine.alcohol], ['Народный рейтинг', wine.rating === null ? null : `${number(wine.rating)} из 5`], ['Цена', wine.price_rub === null ? 'Не указана' : `${number(wine.price_rub)} ₽`]]) {
      if (value) facts.append(element('dt', '', label), element('dd', '', value));
    }
    content.append(facts, element('p', 'description', wine.description || 'Описание отсутствует в источнике.'), sourceLink(wine.source_url, 'Оригинальная карточка на vino-svoe.ru ↗'), element('p', 'source-note', `Сведения от ${new Date(wine.scraped_at).toLocaleDateString('ru-RU')}. Актуальная информация — на странице вина.`));
  } catch (error) {
    const title = element('h2', '', 'Карточка недоступна');
    title.id = 'wine-heading';
    const retry = element('button', 'utility-button', 'Повторить загрузку');
    retry.type = 'button';
    retry.addEventListener('click', () => openDetails(id));
    content.replaceChildren(title, element('p', 'notice', errorText(error)), retry);
  }
}

showInitialCatalog();
loadHistory().catch(() => { /* Историю можно повторно открыть отдельной кнопкой. */ });
