<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { WarningFilled } from '@element-plus/icons-vue';
import { api } from '../api';
import { formatDate, jobLabel } from '../lib';
import StatePanel from '../components/StatePanel.vue';
import StatusTag from '../components/StatusTag.vue';
import PageIntro from '../components/PageIntro.vue';
const loading = ref(true), error = ref(''), refreshError = ref(''), dashboard = ref<any>(), timer = ref<number>(), inFlight = ref(false), lastUpdated = ref('');
const counts = computed(() => dashboard.value?.counts ?? {});
const reviewRequired = computed(() => Number(counts.value.review_required ?? 0));
const failedJobs = computed(() => Number(counts.value.failed_jobs ?? 0));
const attentionText = computed(() => {
    const parts: string[] = [];
    if (reviewRequired.value > 0)
        parts.push(`${reviewRequired.value} 条内容待人工复核`);
    if (failedJobs.value > 0)
        parts.push(`${failedJobs.value} 个任务未完成`);
    return parts.length ? `${parts.join('，')}，建议优先处理。` : '';
});
const countCards = computed(() => [
    { key: 'topics', label: '选题总数', to: '/sources' },
    { key: 'contents', label: '内容总数', to: '/contents' },
    { key: 'review_required', label: '待复核内容', to: '/contents?status=REVIEW_REQUIRED', tone: reviewRequired.value > 0 ? 'attention' : '' },
    { key: 'published', label: '已确认发布', to: '/publications' },
    { key: 'failed_jobs', label: '失败任务', to: '/jobs?status=FAILED', tone: failedJobs.value > 0 ? 'danger' : '' },
]);
const workflowSteps = [
    { name: '选题', desc: '从选题库挑选候选', to: '/sources' },
    { name: '制作', desc: '生成脚本与视频', to: '/contents' },
    { name: '审核', desc: '事实与合规复核', to: '/contents?status=REVIEW_REQUIRED' },
    { name: '发布', desc: '查看发布与设备状态', to: '/publications' },
];
const recentJobs = computed(() => (dashboard.value?.recent_jobs ?? []).slice(0, 5));
const recentContents = computed(() => dashboard.value?.recent_contents ?? []);
const lastUpdatedText = computed(() => lastUpdated.value ? formatDate(lastUpdated.value) : '');
async function load(background = false) {
    if (inFlight.value)
        return;
    inFlight.value = true;
    if (!dashboard.value) {
        loading.value = true;
        if (!background)
            error.value = '';
    }
    try {
        dashboard.value = await api.dashboard();
        error.value = '';
        refreshError.value = '';
        lastUpdated.value = new Date().toISOString();
    }
    catch (e: any) {
        if (dashboard.value)
            refreshError.value = e.message || '刷新失败';
        else
            error.value = e.message || '请检查本地服务是否已启动';
    }
    finally {
        loading.value = false;
        inFlight.value = false;
    }
}
onMounted(() => { load(); timer.value = window.setInterval(() => load(true), 15000); });
onBeforeUnmount(() => timer.value && clearInterval(timer.value));
</script>
<template>
<PageIntro title="今天的生产节奏" description="从选题、生成、人工复核到发布确认，所有环节均在本地保留状态。">
  <span v-if="lastUpdatedText" class="refresh-note">更新于 {{ lastUpdatedText }}</span>
  <el-button :loading="inFlight && !!dashboard" @click="load()">刷新</el-button>
</PageIntro>
<StatePanel :loading="loading && !dashboard" :error="dashboard ? '' : error" @retry="load()">
  <template v-if="dashboard">
    <section v-if="attentionText" class="attention-band" role="status">
      <el-icon aria-hidden="true">
        <WarningFilled />
      </el-icon>
      <p>{{ attentionText }}</p>
      <RouterLink v-if="reviewRequired > 0" to="/contents?status=REVIEW_REQUIRED">
        <el-button size="small" type="warning" plain>去复核</el-button>
      </RouterLink>
      <RouterLink v-if="failedJobs > 0" to="/jobs?status=FAILED">
        <el-button size="small" type="warning" plain>处理失败任务</el-button>
      </RouterLink>
    </section>
    <el-alert
      v-if="refreshError"
      class="refresh-warning"
      type="warning"
      show-icon
      :title="`自动刷新失败，页面仍是 ${lastUpdatedText || '上次成功'} 的数据：${refreshError}`"
      @close="refreshError=''"
    />
    <nav class="workflow" aria-label="生产流程">
      <RouterLink v-for="(step, index) in workflowSteps" :key="step.name" :to="step.to" class="workflow-step">
        <span class="step-no" aria-hidden="true">{{ index + 1 }}</span>
        <span>
          {{ step.name }}
          <small>{{ step.desc }}</small>
        </span>
      </RouterLink>
    </nav>
    <section class="metric-grid" aria-label="总量统计">
      <RouterLink
        v-for="card in countCards"
        :key="card.key"
        :to="card.to"
        class="metric"
        :class="card.tone"
      >
        <span>{{ card.label }}</span>
        <strong>{{ counts[card.key] ?? 0 }}</strong>
      </RouterLink>
    </section>
    <section class="quick-actions" aria-label="快捷操作">
      <RouterLink to="/sources">
        <el-button type="primary" plain>选择选题</el-button>
      </RouterLink>
      <RouterLink to="/contents?status=REVIEW_REQUIRED">
        <el-button plain>复核内容</el-button>
      </RouterLink>
      <RouterLink to="/jobs">
        <el-button plain>查看任务</el-button>
      </RouterLink>
    </section>
    <section class="two-columns">
      <el-card shadow="never">
        <template #header>
          <div class="card-title">
            最近任务
            <RouterLink to="/jobs">查看全部</RouterLink>
          </div>
        </template>
        <el-empty v-if="!recentJobs.length" description="暂无任务，创建内容后可在这里跟踪生成进度。" :image-size="60"/>
        <div v-else class="compact-list job-activity">
          <div v-for="job in recentJobs" :key="job.id" class="line-item">
            <span>
              <b>{{ jobLabel(job.kind) }}</b>
              <small>{{ job.current_step || job.error_message || '等待处理' }} · {{ formatDate(job.updated_at) }}</small>
            </span>
            <StatusTag :status="job.status"/>
          </div>
        </div>
      </el-card>
      <el-card shadow="never">
        <template #header>
          <div class="card-title">
            最近内容
            <RouterLink to="/contents">进入工作台</RouterLink>
          </div>
        </template>
        <el-empty v-if="!recentContents.length" description="暂无内容，先从选题库创建一条。" :image-size="60"/>
        <div v-else class="compact-list">
          <div v-for="content in recentContents" :key="content.id">
            <div>
              <RouterLink :to="`/contents?content_id=${content.id}`">{{ content.title }}</RouterLink>
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
        <small>Token {{ dashboard.usage?.tokens_today ?? 0 }} · 费用 {{ dashboard.usage?.cost ?? '未知' }}，以服务商结算为准</small>
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
