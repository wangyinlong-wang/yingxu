<script setup lang="ts">
import { computed, onMounted } from 'vue';
import { useRoute } from 'vue-router';
import { DataAnalysis, Collection, VideoPlay, List, Cellphone, TrendCharts, Setting, Opportunity } from '@element-plus/icons-vue';
import { useSystemStore } from './stores/system';
const route = useRoute();
const system = useSystemStore();
const title = computed(() => String(route.meta.title || '映序'));
const items = [{ to: '/', label: '总览', icon: DataAnalysis }, { to: '/sources', label: '选题库', icon: Collection }, { to: '/contents', label: '内容工作台', icon: VideoPlay }, { to: '/jobs', label: '任务中心', icon: List }, { to: '/publications', label: '发布与设备', icon: Cellphone }, { to: '/analytics', label: '数据复盘', icon: TrendCharts }, { to: '/strategy', label: '策略与实验', icon: Opportunity }, { to: '/setup', label: '模板与设置', icon: Setting }];
onMounted(() => system.refresh());
</script>
<template>
<el-container class="shell">
  <aside class="sidebar">
    <div class="brand">
      <span>映</span>
      <strong>映序</strong>
    </div>
    <p class="brand-sub">内容运营工作台</p>
    <nav>
      <RouterLink
        v-for="item in items"
        :key="item.to"
        :to="item.to"
        class="nav-item"
      >
        <el-icon>
          <component :is="item.icon" />
        </el-icon>
        {{ item.label }}
      </RouterLink>
    </nav>
    <div class="side-footer">
      <span class="dot" :class="{offline:system.checked && !system.online}" />
      {{ system.online ? '本地服务正常' : system.checked ? '服务未连接' : '正在连接' }}
      <small v-if="system.version">v{{ system.version }}</small>
    </div>
  </aside>
  <el-container direction="vertical" class="main">
    <header class="topbar">
      <div>
        <h1>{{ title }}</h1>
        <p>本地单人内容生产与发布协作</p>
      </div>
      <el-tag type="info" effect="plain">本地环境</el-tag>
    </header>
    <main>
      <RouterView />
    </main>
  </el-container>
</el-container>
</template>
