// No browser, network, credentials or duplicated production state implementation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { test } = require('node:test');
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
assert.ok(fixture.state, 'TikTok Alpine component must exist');
const controls = fixture.controls;
const valid = (overrides = {}) => ({
    available: true, creator_nickname: 'Synthetic creator',
    privacy_level_options: ['PUBLIC_TO_EVERYONE', 'SELF_ONLY'],
    comment_disabled: false, duet_disabled: false, stitch_disabled: false,
    max_video_post_duration_sec: 60, ...overrides,
});
const response = (data) => async () => ({ ok: true, json: async () => data });
const control = (predicate) => {
    const found = controls.filter(predicate);
    assert.equal(found.length, 1, 'Control must resolve uniquely');
    return found[0];
};
const privacy = control(x => x.tag === 'input' && x[':name']?.includes('tiktok_privacy_level_'));
const trigger = control(x => x['x-ref'] === 'trigger');
const option = control(x => x.role === 'option');
const bind = (state, expression, extra = {}) => {
    Object.assign(state, extra);
    return Function('state', `with (state) { return (${expression}); }`)(state);
};
const effect = (state) => Function('state', `with (state) { ${fixture.effect}; }`)(state);
const make = (fetch = response(valid()), extras = {}) => {
    const state = Function('platformExtras', 'accId', 'fetch', `return (${fixture.state});`)({ tt: extras }, 'tt', fetch);
    Object.assign(state, {
        accId: 'tt', charLimits: { tt: { platform: 'tiktok' } },
        tiktokSettingsReady: {}, open: true, $refs: { trigger: { focus() {} } },
    });
    return state;
};
const readyForSubmit = (state, selected = ['tt'], platforms = { tt: { platform: 'tiktok' } }) => {
    const match = fixture.template.match(/get tiktokReadyToSubmit\(\) \{([\s\S]*?)\n        \},/);
    assert.ok(match, 'Parent readiness gate must exist');
    return Function(match[1]).call({ selectedAccounts: selected, charLimits: platforms, tiktokSettingsReady: state.tiktokSettingsReady });
};
const assertBlocked = state => {
    assert.equal(state.ttReady, false);
    assert.deepEqual(state.ttOptions, []);
    assert.equal(bind(state, trigger[':disabled']), true);
    assert.equal(bind(state, privacy[':value']), '');
    effect(state);
    assert.equal(readyForSubmit(state), false);
    for (const field of ['comment', 'duet', 'stitch']) {
        const item = control(x => x.type === 'checkbox' && x[':name']?.includes(`tiktok_allow_${field}_`));
        assert.equal(bind(state, item[':disabled']), true);
    }
};

test('new panel has no invented options or usable privacy before creator lookup', () => {
    const state = make();
    assert.equal(state.ttPrivacy, '');
    assert.equal(state.ttReady, false);
    assert.deepEqual(state.ttOptions, []);
    assert.equal(bind(state, trigger[':disabled']), true);
    assert.equal(bind(state, privacy[':value']), '');
    assert.ok(privacy[':required'].includes('selectedAccounts.includes(accId)'));
});

for (const [name, data] of [
    ['unavailable', { available: false, privacy_level_options: [] }],
    ['null', null], ['empty options', valid({ privacy_level_options: [] })],
    ['string options', valid({ privacy_level_options: 'SELF_ONLY' })],
    ['unknown option', valid({ privacy_level_options: ['UNKNOWN'] })],
    ['mixed unknown option', valid({ privacy_level_options: ['SELF_ONLY', 'UNKNOWN'] })],
    ['duplicate options', valid({ privacy_level_options: ['SELF_ONLY', 'SELF_ONLY'] })],
    ['missing nickname', valid({ creator_nickname: '' })],
    ['missing interaction flag', valid({ comment_disabled: undefined })],
    ['truthy interaction flag', valid({ duet_disabled: 'false' })],
    ['missing duration', valid({ max_video_post_duration_sec: null })],
    ['zero duration', valid({ max_video_post_duration_sec: 0 })],
    ['fractional duration', valid({ max_video_post_duration_sec: 1.5 })],
]) {
    test(`creator response ${name} fails closed`, async () => {
        const state = make(response(data), { privacy_level: 'PUBLIC_TO_EVERYONE' });
        await state.loadTikTokCreatorInfo();
        assertBlocked(state);
        assert.ok(state.ttError);
    });
}

test('network, HTTP and JSON failures remain fixed-text failures without automatic retries', async () => {
    for (const fetch of [
        async () => { throw Error('synthetic private error'); },
        async () => ({ ok: false }),
        async () => ({ ok: true, json() { throw Error('synthetic private error'); } }),
    ]) {
        let calls = 0;
        const state = make(async (...args) => { calls++; return fetch(...args); });
        await state.loadTikTokCreatorInfo();
        assertBlocked(state);
        assert.equal(calls, 1);
        assert.ok(!state.ttError.includes('synthetic private error'));
        assert.equal(state.ttLoading, false);
    }
});

test('sole SELF_ONLY requires an actual manual click before submission', async () => {
    const state = make(response(valid({ privacy_level_options: ['SELF_ONLY'] })));
    await state.loadTikTokCreatorInfo();
    assert.deepEqual(state.ttOptions, ['SELF_ONLY']);
    assert.equal(state.ttSelfOnlyLocked, true);
    assert.equal(state.ttPrivacy, '');
    effect(state);
    assert.equal(readyForSubmit(state), false);
    bind(state, `(() => { ${option['@click']} })()`, { lvl: 'SELF_ONLY' });
    assert.equal(state.ttPrivacy, 'SELF_ONLY');
    assert.equal(bind(state, privacy[':value']), 'SELF_ONLY');
    effect(state);
    assert.equal(readyForSubmit(state), true);
});

test('saved explicit privacy remains only when still allowed; never substitutes another choice', async () => {
    for (const [saved, expected] of [['SELF_ONLY', 'SELF_ONLY'], ['PUBLIC_TO_EVERYONE', '']]) {
        const state = make(response(valid({ privacy_level_options: ['SELF_ONLY'] })), { privacy_level: saved });
        await state.loadTikTokCreatorInfo();
        assert.equal(state.ttPrivacy, expected);
    }
});

test('explicit retry recovers from unavailable settings without inventing a privacy choice', async () => {
    let calls = 0;
    const state = make(async () => ({ ok: true, json: async () => ++calls === 1 ? { available: false } : valid() }));
    await state.loadTikTokCreatorInfo();
    assertBlocked(state);
    await state.loadTikTokCreatorInfo();
    assert.equal(calls, 2);
    assert.equal(state.ttError, '');
    assert.equal(state.ttReady, true);
    assert.equal(state.ttPrivacy, '');
    effect(state);
    assert.equal(readyForSubmit(state), false);
});

test('pending retry disables previously ready controls and refuses concurrent requests', async () => {
    let resolve;
    let calls = 0;
    const fetch = async () => {
        calls++;
        if (calls === 1) return { ok: true, json: async () => valid() };
        return new Promise(done => { resolve = done; });
    };
    const state = make(fetch, { privacy_level: 'SELF_ONLY' });
    await state.loadTikTokCreatorInfo();
    effect(state);
    assert.equal(readyForSubmit(state), true);
    const pending = state.loadTikTokCreatorInfo();
    assertBlocked(state);
    await state.loadTikTokCreatorInfo();
    assert.equal(calls, 2);
    bind(state, `(() => { ${option['@click']} })()`, { lvl: 'PUBLIC_TO_EVERYONE' });
    assert.equal(state.ttPrivacy, 'SELF_ONLY');
    resolve({ ok: true, json: async () => valid() });
    await pending;
    assert.equal(state.ttReady, true);
});

test('saved advanced settings survive unavailable lookup; interaction mirrors preserve drafts', async () => {
    const extras = { privacy_level: 'SELF_ONLY', disable_comment: false, disable_duet: false,
        disable_stitch: false, brand_organic_toggle: true, is_aigc: true, video_cover_timestamp_ms: 1234 };
    const state = make(response({ available: false }), extras);
    await state.loadTikTokCreatorInfo();
    assertBlocked(state);
    assert.equal(state.ttBrandOrganic, true);
    assert.equal(state.ttAigc, true);
    assert.equal(extras.video_cover_timestamp_ms, 1234);
    for (const field of ['comment', 'duet', 'stitch']) {
        const mirror = control(x => x.type === 'hidden' && x[':name']?.includes(`tiktok_allow_${field}_`));
        assert.equal(bind(state, mirror[':disabled']), false);
        assert.equal(mirror.value, 'true');
    }
});

test('creator-disabled interactions clear only their own toggles after valid lookup', async () => {
    const state = make(response(valid({ duet_disabled: true })), {
        disable_comment: false, disable_duet: false, disable_stitch: false,
    });
    await state.loadTikTokCreatorInfo();
    assert.equal(state.ttAllowComment, true);
    assert.equal(state.ttAllowDuet, false);
    assert.equal(state.ttAllowStitch, true);
    for (const field of ['comment', 'duet', 'stitch']) {
        const mirror = control(x => x.type === 'hidden' && x[':name']?.includes(`tiktok_allow_${field}_`));
        assert.equal(bind(state, mirror[':disabled']), true);
    }
});

test('unknown selection cannot enter the privacy field; branded content cannot use SELF_ONLY', async () => {
    const state = make();
    await state.loadTikTokCreatorInfo();
    bind(state, `(() => { ${option['@click']} })()`, { lvl: 'UNKNOWN' });
    assert.equal(state.ttPrivacy, '');
    state.ttPrivacy = 'SELF_ONLY';
    state.ttBrandContent = true;
    assert.equal(bind(state, option[':disabled'], { lvl: 'SELF_ONLY' }), true);
    assert.equal(bind(state, privacy[':value']), '');
});

test('multi-account gate waits for every selected TikTok account but ignores other platforms and deselections', () => {
    const state = make();
    state.tiktokSettingsReady = { tt: true, second: false };
    const platforms = { tt: { platform: 'tiktok' }, second: { platform: 'tiktok' }, fb: { platform: 'facebook' } };
    assert.equal(readyForSubmit(state, ['tt', 'second'], platforms), false);
    assert.equal(readyForSubmit(state, ['tt', 'fb'], platforms), true);
    assert.equal(readyForSubmit(state, ['fb'], platforms), true);
    for (const action of ['submit_for_approval', 'resubmit_for_approval', 'add_to_queue']) {
        const button = control(x => x.tag === 'button' && x.value === action);
        assert.equal(bind({ selectedAccounts: ['tt'], tiktokReadyToSubmit: false }, button[':disabled']), true);
        assert.equal(bind({ selectedAccounts: ['fb'], tiktokReadyToSubmit: true }, button[':disabled']), false);
    }
});
