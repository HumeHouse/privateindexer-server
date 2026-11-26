'use strict';

const $ = id => document.getElementById(id);
const filter = $('filter');
const refresh = $('refresh');
const status = $('status');
const rows = $('user-rows');
const actionDialog = $('action-dialog');
const keyDialog = $('key-dialog');
const submit = $('submit-action');
const cancel = $('cancel-action');
const label = $('user-label');
let users = [];
let loading = false;
let saving = false;
let selected;
let loadError = '';
let notificationTimer;
let notificationOrigin;

function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return '0 B';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB'];
    const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    return (bytes / (1024 ** i)).toFixed(1) + ' ' + units[i];
}

function reach(user) {
    return user.reachable === 1 ? 'Reachable' : user.reachable === 0 ? 'Unreachable' : 'Unknown';
}

function cell(row, value = '', className = '') {
    const td = row.insertCell();
    td.textContent = value ?? '';
    td.className = className;
    return td;
}

function rowButton(text, description, handler, danger = false) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = text;
    button.title = description;
    button.setAttribute('aria-label', description);
    button.className = 'button small' + (danger ? ' danger' : '');
    button.addEventListener('click', handler);
    return button;
}

function renderUsers() {
    const query = filter.value.trim().toLowerCase();
    const visible = users.filter(user => String(user.id).includes(query) || String(user.label ?? '').toLowerCase().includes(query));
    const fragment = document.createDocumentFragment();
    for (const user of visible) {
        const row = document.createElement('tr');
        fragment.append(row);
        cell(row, user.id, 'mono');
        cell(row, user.label, 'identity');
        const connection = reach(user);
        const badge = document.createElement('span');
        badge.className = 'badge ' + (connection === 'Reachable' ? 'good' : connection === 'Unreachable' ? 'bad' : '');
        badge.textContent = connection;
        cell(row).append(badge);
        const keys = document.createElement('div');
        keys.className = 'key-actions';
        keys.append(
            rowButton('Copy key', `Copy API key for ${user.label}`, () => void copyKey(user)),
            rowButton('Rotate', `Rotate API key for ${user.label}`, () => showAction('rotate', user)),
        );
        cell(row).append(keys);
        for (const value of [formatBytes(user.downloaded), formatBytes(user.uploaded), user.seeding, user.leeching, user.popularity, user.grabs]) {
            cell(row, value, 'number');
        }
        cell(row, user.client_version || '-');
        cell(row, user.last_seen_ago || '-').title = user.last_seen || '-';
        cell(row, user.last_ip || '-', 'mono');
        cell(row).append(rowButton('Delete', `Delete user ${user.label}`, () => showAction('delete', user), true));
    }
    rows.replaceChildren(fragment);
    const caption = !users.length ? 'No users yet.' : !visible.length ? 'No users match that filter.' :
        `Showing ${visible.length} of ${users.length} ${users.length === 1 ? 'user' : 'users'}.`;
    status.classList.toggle('is-error', Boolean(loadError));
    status.textContent = loadError || caption;
}

function errorMessage(error, fallback) {
    return error.status === 401 ? 'Your session expired. Reload the page to sign in again.' : fallback;
}

async function request(path, options) {
    const response = await fetch(path, {cache: 'no-store', ...options});
    if (!response.ok) throw Object.assign(new Error('Request failed'), {status: response.status});
    return response;
}

async function loadUsers() {
    if (loading) return;
    loading = true;
    refresh.disabled = true;
    $('open-create').disabled = true;
    rows.inert = true;
    status.classList.remove('is-error');
    status.textContent = 'Loading users...';
    try {
        const data = await (await request('/admin/users')).json();
        if (!Array.isArray(data)) throw new Error('Invalid user list');
        users = data;
        const counts = {Reachable: 0, Unreachable: 0, Unknown: 0};
        for (const user of users) counts[reach(user)]++;
        $('count-total').textContent = users.length;
        for (const [name, count] of Object.entries(counts)) $(`count-${name.toLowerCase()}`).textContent = count;
        filter.disabled = false;
        loadError = '';
    } catch (error) {
        // Clear stale rows rather than leaving potentially rotated keys available.
        users = [];
        filter.disabled = true;
        for (const name of ['total', 'reachable', 'unreachable', 'unknown']) $(`count-${name}`).textContent = '\u2013';
        loadError = errorMessage(error, 'Could not load users. Try Refresh.');
    } finally {
        loading = false;
        refresh.disabled = false;
        $('open-create').disabled = false;
        rows.inert = false;
        renderUsers();
    }
}

function showAction(kind, user) {
    selected = {kind, user};
    const create = kind === 'create';
    const rotate = kind === 'rotate';
    $('action-title').textContent = create ? 'Create user' : rotate ? 'Rotate API key' : 'Delete user';
    $('action-description').textContent = create ? 'Enter a label for the new user.' : rotate ?
        `Rotate the API key for ${user.label}? The current key will stop working immediately.` :
        `Delete ${user.label}? This cannot be undone.`;
    $('label-field').hidden = !create;
    label.disabled = !create;
    label.required = create;
    label.value = '';
    $('action-error').hidden = true;
    submit.className = 'button ' + (create ? 'primary' : 'danger-solid');
    submit.textContent = create ? 'Create user' : rotate ? 'Rotate key' : 'Delete user';
    actionDialog.showModal();
    (create ? label : cancel).focus();
}

$('action-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (saving || !selected) return;
    const {kind, user} = selected;
    const path = user ? `/admin/user/${encodeURIComponent(user.id)}` : '/admin/user';
    const fields = kind === 'create' ? {user_label: label.value} : {rotate_key: 'true'};
    const options = kind === 'delete' ? {method: 'DELETE'} : {method: 'POST', body: new URLSearchParams(fields)};
    saving = true;
    submit.disabled = cancel.disabled = true;
    const caption = submit.textContent;
    submit.textContent = 'Saving...';
    $('action-error').hidden = true;
    try {
        await request(path, options);
        actionDialog.close();
        notify(kind === 'create' ? 'User created.' : kind === 'rotate' ? 'API key rotated.' : 'User deleted.');
        await loadUsers();
        $(kind === 'create' ? 'open-create' : 'users-heading').focus();
    } catch (error) {
        $('action-error').textContent = errorMessage(error, 'Could not confirm the change. Refresh the user list before trying again.');
        $('action-error').hidden = false;
    } finally {
        saving = false;
        submit.disabled = cancel.disabled = false;
        submit.textContent = caption;
    }
});

const notification = $('notification');

function hideNotification(restoreFocus = false) {
    clearTimeout(notificationTimer);
    if (restoreFocus && notification.contains(document.activeElement)) {
        (notificationOrigin?.isConnected ? notificationOrigin : $('users-heading')).focus();
    }
    notification.hidden = true;
    $('notification-announcement').textContent = '';
}

function scheduleNotificationDismissal() {
    clearTimeout(notificationTimer);
    if (!notification.matches(':hover, :focus-within')) {
        notificationTimer = setTimeout(() => hideNotification(), 5500);
    }
}

function notify(message) {
    if (!notification.contains(document.activeElement)) notificationOrigin = document.activeElement;
    $('notification-message').textContent = message;
    $('notification-announcement').textContent = message;
    notification.hidden = false;
    scheduleNotificationDismissal();
}

function showKey(user, message) {
    $('key-label').textContent = `API key for ${user.label}`;
    $('full-key').value = user.api_key ?? '';
    $('key-status').textContent = message;
    if (!keyDialog.open) keyDialog.showModal();
    if (user.api_key) {
        $('full-key').focus();
        $('full-key').select();
    }
}

async function copyKey(user) {
    if (!user.api_key) return showKey(user, 'No API key is available.');
    try {
        await navigator.clipboard.writeText(user.api_key);
        notify('API key copied.');
    } catch {
        if (!loading && users.includes(user) && !actionDialog.open) {
            showKey(user, 'Automatic copy is unavailable. Select and copy the key manually.');
        }
    }
}

$('dismiss-notification').addEventListener('click', () => hideNotification(true));
for (const event of ['pointerenter', 'focusin']) {
    notification.addEventListener(event, () => clearTimeout(notificationTimer));
}
for (const event of ['pointerleave', 'focusout']) {
    notification.addEventListener(event, () => queueMicrotask(scheduleNotificationDismissal));
}
keyDialog.addEventListener('close', () => { if (!keyDialog.open) $('full-key').value = ''; });
actionDialog.addEventListener('close', () => { if (!actionDialog.open) selected = undefined; });
actionDialog.addEventListener('cancel', event => { if (saving) event.preventDefault(); });
cancel.addEventListener('click', () => actionDialog.close());
$('open-create').addEventListener('click', () => showAction('create'));
refresh.addEventListener('click', () => void loadUsers());
filter.addEventListener('input', () => { if (!loading) renderUsers(); });
void loadUsers();
