import { createRouter, createWebHashHistory } from 'vue-router'
import HomeView from './views/HomeView.vue'
import EventsView from './views/EventsView.vue'
import DetailView from './views/DetailView.vue'
import DeviceView from './views/DeviceView.vue'
import ContactView from './views/ContactView.vue'

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'home', component: HomeView, meta: { title: '家人近况' } },
    { path: '/events', name: 'events', component: EventsView, meta: { title: '事件' } },
    { path: '/events/:id', name: 'detail', component: DetailView, meta: { title: '事件详情' } },
    { path: '/history', name: 'history', component: EventsView, meta: { title: '历史记录' } },
    { path: '/device', name: 'device', component: DeviceView, meta: { title: '设备状态' } },
    { path: '/device/contact', name: 'contact', component: ContactView, meta: { title: '家庭联系电话' } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

router.afterEach(to => {
  document.title = `${String(to.meta.title || '家人近况')} · 家属端`
})
