<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { ArrowLeft, Phone } from '@lucide/vue'
import { api, type ContactState } from '../api'
import { usePortalStore } from '../stores/portal'

const portal = usePortalStore()
const state = ref<ContactState | null>(null)
const draft = ref(''), error = ref(''), notice = ref(''), busy = ref(false)
let request = 0
let timer: number | undefined

function show(value: ContactState) {
  state.value = value
  portal.updateContactPhone(value.phone)
}
async function refresh() {
  if (portal.directAccess || !portal.paired || busy.value) return
  const run = ++request
  try {
    const value = await api<ContactState>('/contact')
    if (run === request && portal.paired) show(value)
  } catch (reason) {
    if (run === request && !state.value) error.value = reason instanceof Error ? reason.message : '无法读取联系电话。'
  }
}
async function change(path: string, body: Record<string, string>, message: string) {
  if (busy.value) return
  const run = ++request
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const value = await api<ContactState>(path, body)
    if (run !== request || !portal.paired) return
    show(value); notice.value = message; draft.value = ''
  } catch (reason) {
    if (run === request) error.value = reason instanceof Error ? reason.message : '操作未完成，请重试。'
  } finally {
    if (run === request) {
      busy.value = false
      if (error.value) void refresh()
    }
  }
}
function propose() {
  const phone = draft.value.trim().replace(/[ ()-]/g, '')
  if (!/^\+?[0-9]{3,20}$/.test(phone)) {
    error.value = '请输入有效的联系电话，可在号码前加国际区号。'
    return
  }
  if (portal.directAccess) {
    error.value = portal.saveLocalPhone(phone)
    notice.value = error.value ? '' : '号码已保存。'
    return
  }
  void change('/contact/propose', { phone }, '申请已提交，等待其他家属同意。')
}
function operate(action: 'approve' | 'apply' | 'cancel', message: string) {
  if (state.value?.pending) void change('/contact/' + state.value.pending.id + '/' + action, {}, message)
}
watch(() => portal.paired, paired => {
  if (portal.directAccess) return
  request++; state.value = null; busy.value = false; draft.value = ''; error.value = ''; notice.value = ''
  if (paired) void refresh()
})
watch(() => portal.contactPhone, phone => {
  if (portal.directAccess && !draft.value) draft.value = phone
}, { immediate: true })
onMounted(() => {
  void refresh()
  if (!portal.directAccess) timer = window.setInterval(refresh, 5000)
})
onUnmounted(() => { request++; window.clearInterval(timer) })
</script>

<template>
  <RouterLink class="back-link" to="/device"><ArrowLeft :size="18" />返回设备</RouterLink>
  <div class="page-heading"><div><h1>联系电话</h1><p v-if="!portal.directAccess">所有已配对家属共用此号码。</p></div></div>
  <section v-if="!portal.paired && !portal.directAccess" class="page-empty">
    <h2>请先配对手机</h2><p>配对后可查看和修改家庭联系电话。</p><RouterLink class="button primary" to="/">前往配对</RouterLink>
  </section>
  <section v-else class="contact-settings">
    <div class="contact-current"><div><h2>当前号码</h2><p>{{ portal.directAccess ? portal.contactPhone || '尚未设置' : state?.phone || (state ? '尚未设置' : '正在读取') }}</p></div>
      <a v-if="portal.contactPhone" class="button secondary" :href="'tel:' + portal.contactPhone"><Phone :size="17" />打开手机拨号</a>
    </div>
    <div v-if="state?.pending" class="contact-request">
      <h2>号码修改申请</h2>
      <dl class="fact-list">
        <div><dt>申请人</dt><dd>{{ state.pending.requester }}</dd></div>
        <div><dt>新号码</dt><dd>{{ state.pending.phone }}</dd></div>
        <div><dt>同意进度</dt><dd>{{ state.pending.approved }} / {{ state.pending.required }}</dd></div>
      </dl>
      <div class="contact-actions">
        <button v-if="state.pending.can_approve" class="button primary" :disabled="busy" @click="operate('approve', '已记录你的同意。')">同意修改</button>
        <button v-if="state.pending.mine" class="button primary" :disabled="busy || !state.pending.can_apply" @click="operate('apply', '家庭联系电话已更新。')">应用新号码</button>
        <button v-if="state.pending.mine" class="button secondary" :disabled="busy" @click="operate('cancel', '修改申请已撤回。')">撤回申请</button>
      </div>
      <p class="footnote">{{ state.pending.i_approved ? '你已同意。' : '' }}{{ state.pending.required ? '所有其他已配对手机同意后，由申请人应用新号码。' : '需要另一台已配对手机同意，当前号码保留。' }}</p>
    </div>
    <form v-else-if="portal.directAccess || state?.can_propose" class="handling-form contact-form" @submit.prevent="propose">
      <label for="family-phone">{{ portal.directAccess ? '编辑号码' : '新联系电话' }}</label>
      <input id="family-phone" v-model="draft" class="text-field" type="tel" inputmode="tel" maxlength="32" autocomplete="tel" required :disabled="busy" />
      <p v-if="!portal.directAccess" class="footnote">所有其他已配对手机同意后，你可以应用新号码。</p>
      <button type="submit" class="button primary" :disabled="busy || !draft.trim()">{{ portal.directAccess ? '保存号码' : '申请修改' }}</button>
    </form>
    <p v-else-if="state" class="contact-request">修改号码需要至少两台已配对手机。当前号码保留。</p>
    <p v-if="notice" role="status" class="contact-feedback">{{ notice }}</p>
    <p v-if="error" role="alert" class="form-error contact-feedback">{{ error }}</p>
  </section>
</template>
