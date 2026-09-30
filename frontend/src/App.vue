<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { DataAnalysis, Collection, VideoPlay, List, Cellphone, TrendCharts, Setting, Opportunity, Menu, Close } from '@element-plus/icons-vue';
import { useSystemStore } from './stores/system';
const route = useRoute();
const system = useSystemStore();
const navOpen = ref(false);
const title = computed(() => String(route.meta.title || '映序'));
const groups = [
  { label: '创作', items: [{ to: '/', label: '总览', icon: DataAnalysis }, { to: '/sources', label: '选题库', icon: Collection }, { to: '/contents', label: '内容工作台', icon: VideoPlay }] },
  { label: '运营', items: [{ to: '/jobs', label: '任务中心', icon: List }, { to: '/publications', label: '发布与设备', icon: Cellphone }] },
  { label: '增长', items: [{ to: '/analytics', label: '数据复盘', icon: TrendCharts }, { to: '/strategy', label: '策略与实验', icon: Opportunity }] },
];
const setupItem = { to: '/setup', label: '模板与设置', icon: Setting };
const statusText = computed(() => system.online ? '本地服务在线' : system.checked ? '服务离线' : '状态未知');
const statusType = computed(() => system.online ? 'success' : system.checked ? 'danger' : 'info');
watch(() => route.fullPath, () => { navOpen.value = false; });
function closeNavAt(target: string) { if (route.path === target) navOpen.value = false; }
onMounted(() => system.refresh());
</script>
<template>
<a class="skip-link" href="#main-content">跳到主要内容</a>
<el-container class="shell">
  <aside class="sidebar">
    <div class="brand">
      <span aria-hidden="true">映</span>
      <strong>映序</strong>
    </div>
    <p class="brand-sub">内容运营工作台</p>
    <nav aria-label="主导航">
      <div v-for="group in groups" :key="group.label" class="nav-group">
        <span class="nav-group-label">{{ group.label }}</span>
        <RouterLink
          v-for="item in group.items"
          :key="item.to"
          :to="item.to"
          class="nav-item"
        >
          <el-icon>
            <component :is="item.icon" />
          </el-icon>
          {{ item.label }}
        </RouterLink>
      </div>
    </nav>
    <div class="side-footer">
      <RouterLink :to="setupItem.to" class="nav-item">
        <el-icon>
          <component :is="setupItem.icon" />
        </el-icon>
        {{ setupItem.label }}
      </RouterLink>
      <div class="service-status">
        <span class="service-line"><span class="dot" :class="{online:system.online, offline:system.checked && !system.online}" />{{ statusText }}</span>
        <small v-if="system.version">v{{ system.version }}</small>
      </div>
    </div>
  </aside>
  <el-container direction="vertical" class="main">
    <header class="topbar">
      <div class="topbar-context">
        <el-button class="menu-button" aria-label="打开导航菜单" :aria-expanded="navOpen ? 'true' : 'false'" @click="navOpen=true">
          <el-icon>
            <Menu />
          </el-icon>
        </el-button>
        <nav class="breadcrumb" aria-label="当前位置">
          <span>工作台</span>
          <span class="sep" aria-hidden="true">/</span>
          <strong>{{ title }}</strong>
        </nav>
      </div>
      <div class="topbar-status">
        <el-tag :type="statusType" effect="light" size="small">{{ statusText }}</el-tag>
        <el-tag type="info" effect="plain" size="small">本地环境</el-tag>
      </div>
    </header>
    <main id="main-content" tabindex="-1">
      <RouterView />
    </main>
  </el-container>
</el-container>
<el-drawer
  v-model="navOpen"
  direction="ltr"
  size="270px"
  :with-header="false"
  class="nav-drawer"
  aria-label="导航菜单"
>
  <template v-if="navOpen">
    <div class="nav-drawer-head">
      <div class="brand">
        <span aria-hidden="true">映</span>
        <strong>映序</strong>
      </div>
      <el-button aria-label="关闭导航菜单" @click="navOpen=false">
        <el-icon>
          <Close />
        </el-icon>
      </el-button>
    </div>
    <nav aria-label="移动端导航" style="margin-top:16px">
    <div v-for="group in groups" :key="group.label" class="nav-group">
      <span class="nav-group-label">{{ group.label }}</span>
      <RouterLink
        v-for="item in group.items"
        :key="item.to"
        :to="item.to"
        class="nav-item"
        @click="closeNavAt(item.to)"
      >
        <el-icon>
          <component :is="item.icon" />
        </el-icon>
        {{ item.label }}
      </RouterLink>
    </div>
    <RouterLink :to="setupItem.to" class="nav-item" @click="closeNavAt(setupItem.to)">
      <el-icon>
        <component :is="setupItem.icon" />
      </el-icon>
      {{ setupItem.label }}
    </RouterLink>
  </nav>
  </template>
</el-drawer>
</template>
