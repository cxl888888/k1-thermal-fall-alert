<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { House, ListTodo, History, Monitor, Shield, Wifi, WifiOff } from '@lucide/vue'
import { usePortalStore } from './stores/portal'
const portal = usePortalStore(), route = useRoute()
const navigation = [
  { to: '/', label: '首页', icon: House }, { to: '/events', label: '事件', icon: ListTodo },
  { to: '/history', label: '历史', icon: History }, { to: '/device', label: '设备', icon: Monitor },
]
const activeSection = computed(() => route.path.startsWith('/events') ? '/events' :
  route.path.startsWith('/device') ? '/device' : route.path)
onMounted(() => { void portal.restoreSession() })
</script>

<template>
  <a class="skip-link" href="#main-content">跳到主要内容</a>
  <div class="app-shell">
    <header class="app-header">
      <RouterLink to="/" class="brand"><span class="brand-mark"><Shield :size="23" /></span><span>家属端<span class="brand-sub">家人的消息，在这里看见</span></span></RouterLink>
      <span class="connection-pill"><Wifi v-if="portal.backendConnected" :size="15" /><WifiOff v-else :size="15" />{{ portal.backendConnected ? '服务已连接' : portal.error ? '连接中断' : portal.paired ? '正在同步' : portal.directAccess ? '正在连接' : '尚未配对' }}</span>
    </header>
    <div class="workspace">
      <nav class="main-nav" aria-label="主导航"><RouterLink v-for="item in navigation" :key="item.to" :to="item.to" :class="{ selected: activeSection === item.to }" :aria-current="activeSection === item.to ? 'page' : undefined"><component :is="item.icon" :size="22" /><span>{{ item.label }}</span><span v-if="item.to === '/events' && portal.active.length" class="nav-count">{{ portal.active.length }}</span></RouterLink><p class="nav-note">{{ portal.isService ? portal.clientLabel : '设备消息与处置' }}<br />查看每条消息的实际来源</p></nav>
      <main id="main-content" tabindex="-1"><p v-if="portal.error" class="connection-error" role="alert">{{ portal.error }}</p><RouterView /></main>
    </div>
    <footer class="app-footer">{{ portal.isService ? '告警解除以设备有效应答为准' : '家属端 · 尚未建立设备连接' }}</footer>
  </div>
</template>
