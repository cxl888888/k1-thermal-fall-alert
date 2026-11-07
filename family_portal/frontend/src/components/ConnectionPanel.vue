<script setup lang="ts">
import { ref } from 'vue'
import { usePortalStore } from '../stores/portal'
const portal = usePortalStore(), code = ref(''), message = ref('')
async function connect() {
  message.value = await portal.pair(code.value)
  code.value = ''
}
</script>

<template>
  <template v-if="portal.directAccess">
    <button v-if="portal.error && !portal.paired" type="button" class="button secondary" @click="portal.restoreSession()">重新连接</button>
  </template>
  <form v-else-if="portal.backendAvailable && !portal.paired" class="handling-form connection-form" @submit.prevent="connect">
    <label for="pair-code">手机配对码</label>
    <input id="pair-code" v-model="code" type="password" autocomplete="off" spellcheck="false" minlength="24" maxlength="128" required :disabled="portal.busy" aria-describedby="pair-hint" />
    <p id="pair-hint" class="footnote">使用设备管理员提供的一次性配对码。每个手机独立授权，可单独撤销。</p>
    <p v-if="message" class="form-error" role="alert">{{ message }}</p>
    <button class="button primary" type="submit" :disabled="portal.busy">{{ portal.busy ? '正在配对…' : '配对并查看' }}</button>
  </form>
  <p v-else-if="!portal.backendAvailable" class="footnote">请使用设备提供的家属端地址完成配对。</p>
</template>
