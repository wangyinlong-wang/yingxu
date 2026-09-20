<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { api } from '../api';
import { formatDate, jobLabel } from '../lib';
import StatePanel from '../components/StatePanel.vue';
import StatusTag from '../components/StatusTag.vue';
import PageIntro from '../components/PageIntro.vue';
const loading = ref(true), error = ref(''), dashboard = ref<any>(), timer = ref<number>();
async function load() { loading.value = true; error.value = ''; try {
    dashboard.value = await api.dashboard();
}
catch (e: any) {
    error.value = e.message || '请检查本地服务是否已启动';
}
finally {
    loading.value = false;
} }
onMounted(() => { load(); timer.value = window.setInterval(load, 15000); });
onBeforeUnmount(() => timer.value && clearInterval(timer.value));
</script>
<template>
<PageIntro title="今天的生产节奏" description="从选题、生成、人工复核到发布确认，所有环节均在本地保留状态。">
  <el-button @click="load">刷新</el-button>
</PageIntro>
<StatePanel :loading :error @retry="load">
  <template v-if="dashboard">
    <section class="metric-grid">
      <article
        v-for="([key,label]) in [['topics','选题总数'],['contents','内容总数'],['review_required','待复核'],['published','已确认发布'],['failed_jobs','失败任务']]"
        :key="key"
        class="metric"
      >
        <span>{{ label }}</span>
        <strong>{{ dashboard.counts?.[key] ?? 0 }}</strong>
      </article>
    </section>
    <section class="two-columns">
      <el-card shadow="never">
        <template #header>
          <div class="card-title">
            最近任务
            <RouterLink to="/jobs">查看全部</RouterLink>
          </div>
        </template>
        <el-empty v-if="!dashboard.recent_jobs?.length" description="暂无任务，创建内容后可在这里跟踪生成进度。" :image-size="60"/>
        <el-timeline v-else>
          <el-timeline-item
            v-for="job in dashboard.recent_jobs"
            :key="job.id"
            :timestamp="formatDate(job.updated_at)"
            :type="job.status==='FAILED'?'danger':job.status==='SUCCEEDED'?'success':'primary'"
          >
            <b>{{ jobLabel(job.kind) }}</b>
            <StatusTag :status="job.status"/>
            <p class="muted">{{ job.current_step || job.error_message || '等待处理' }}</p>
          </el-timeline-item>
        </el-timeline>
      </el-card>
      <el-card shadow="never">
        <template #header>
          <div class="card-title">
            最近内容
            <RouterLink to="/contents">进入工作台</RouterLink>
          </div>
        </template>
        <el-empty v-if="!dashboard.recent_contents?.length" description="暂无内容，先从选题库创建一条。" :image-size="60"/>
        <div v-else class="compact-list">
          <div v-for="content in dashboard.recent_contents" :key="content.id">
            <div>
              <b>{{ content.title }}</b>
              <p>{{ formatDate(content.updated_at) }}</p>
            </div>
            <StatusTag :status="content.status"/>
          </div>
        </div>
      </el-card>
    </section>
    <section class="usage">
      <el-card shadow="never">
        <span>今日模型/数据调用</span>
        <b>{{ dashboard.usage?.calls_today ?? 0 }}</b>
        <small>Token {{ dashboard.usage?.tokens_today ?? 0 }} · 费用由服务商侧实际结算</small>
      </el-card>
      <el-card shadow="never">
        <span>运行环境</span>
        <b>{{ dashboard.environment?.timezone || '本机运行' }}</b>
        <small>{{ dashboard.environment?.warnings?.[0] || '暂无诊断警告' }}</small>
      </el-card>
    </section>
  </template>
</StatePanel>
</template>
