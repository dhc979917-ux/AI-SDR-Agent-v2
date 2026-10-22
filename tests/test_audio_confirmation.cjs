const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/audio-roles.js'), 'utf8'), context);
const {audioDraftReviewState, setAudioDraftRole} = context;

test('confirm a specific repeated line without changing any other text', () => {
  const source = '待确认：嗯。\n客户：我想试用。\n待确认：嗯。';
  const edited = setAudioDraftRole(source, 2, '销售', '待确认：嗯。');
  assert.equal(edited, '待确认：嗯。\n客户：我想试用。\n销售：嗯。');
  assert.equal(audioDraftReviewState(edited).pending.length, 1);
  assert.equal(audioDraftReviewState(edited).valid, false);
});

test('system announcement can be confirmed as other while keeping customer speech', () => {
  const source = '待确认：来电提示。\n销售：您好。\n客户：我想试用。';
  const edited = setAudioDraftRole(source, 0, '其他', '待确认：来电提示。');
  const state = audioDraftReviewState(edited);
  assert.equal(state.valid, true);
  assert.equal(state.other, 1);
  assert.equal(state.pending.length, 0);
  assert.equal(edited, '其他：来电提示。\n销售：您好。\n客户：我想试用。');
});

test('outdated row, invalid role and attempts to bypass customer requirement are rejected', () => {
  assert.throws(() => setAudioDraftRole('待确认：新的内容。', 0, '销售', '待确认：旧内容。'));
  assert.throws(() => setAudioDraftRole('待确认：播报。', 0, '随便', '待确认：播报。'));
  for (const text of ['其他：播报。', '销售：您好。\n其他：播报。', '客户：', '客户：真实需求\n说话人B：未确认']) {
    assert.equal(audioDraftReviewState(text).valid, false);
  }
});

test('manual edits immediately add or clear pending markers and keep compatible aliases', () => {
  assert.equal(audioDraftReviewState('待确认：您好。\n顾客: 想了解一下。').pending.length, 1);
  assert.equal(audioDraftReviewState('客服: 您好。\n顾客: 想了解一下。').valid, true);
  assert.equal(audioDraftReviewState('客户：有效\n待确认：').valid, false);
});

test('explicitly keeping uncertainty permits other confirmed customer speech to be analyzed', () => {
  const source='待确认：嗯。\n客户：我需要了解价格。';
  const edited=setAudioDraftRole(source,0,'存疑','待确认：嗯。');
  assert.equal(edited,'存疑：嗯。\n客户：我需要了解价格。');
  assert.equal(audioDraftReviewState(edited).valid,true);
  assert.equal(audioDraftReviewState('存疑：我需要了解价格。').valid,false);
});

test('automatic other skips filler review and preserves confirmed drafts and substantive speech', () => {
  const draft='待确认：酷家乐来电。\n客户：嗯嗯。\n客户：嗯，我们下周试用。';
  const expected='其他：酷家乐来电。\n其他：嗯嗯。\n客户：嗯，我们下周试用。';
  assert.equal(context.autoOtherDraft(draft),expected);
  assert.equal(context.audioDraftFor({confirmed_text:draft}),draft);
  assert.equal(context.audioDraftFor({role_status:'ready',role_result:{transcript:draft}}),expected);
  assert.equal(audioDraftReviewState(expected).pending.length,0);
});


test('short greetings and repeated acknowledgements auto classify without swallowing business statements', () => {
  for(const text of ['喂？','哎！','诶。','欸','对对','对，对，对。','嗯，对对。']){
    assert.equal(context.autoOtherDraft('待确认：'+text),'其他：'+text);
  }
  for(const text of ['对','好的','不对','喂，我们想试用','对对，预算五万','哎，下周联系']){
    assert.equal(context.autoOtherDraft('客户：'+text),'客户：'+text);
  }
});
