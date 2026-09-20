<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { api } from '../api';
import { actionKey, formatDate, jobLabel, statusLabel } from '../lib';
import type { Job, JobTimelineItem } from '../types';
import PageIntro from '../components/PageIntro.vue';
import StatePanel from '../components/StatePanel.vue';
import StatusTag from '../components/StatusTag.vue';
const jobs = ref<Job[]>([]), loading = ref(false), error = ref(''), status = ref(''), detail = ref<Job | null>(null), timer = ref<number>();
const resolveDialog = ref(false), resolving = ref<Job | null>(null), resolution = ref<'not_completed' | 'cancel'>('not_completed'), confirmed = ref(false), notes = ref('');
const isPublication = (job: Job) => job.kind.toLowerCase().includes('publication') || job.kind.toLowerCase().includes('publish');
const requiresProviderResolution = (job: Job) => job.status !== 'RUNNING' && (!!job.requires_reconciliation || (job.status === 'WAITING_USER' && !isPublication(job)));
const displayTimeline = (job:Job):JobTimelineItem[] => job.timeline?.length ? job.timeline : job.steps.map(step => ({at:step.completed_at || step.started_at || job.updated_at,kind:'step',name:step.name,status:step.status,detail:step.detail}));
const costLabel = (value:number|null|undefined) => value == null ? '未知' : `$${value}`;
async function load() { loading.value = true; try {
    jobs.value = (await api.jobs(status.value)).items;
    if (detail.value) {
        const fresh = jobs.value.find(j => j.id === detail.value?.id);
        if (fresh)
            detail.value = fresh;
    }
}
catch (e: any) {
    error.value = e.message;
}
finally {
    loading.value = false;
} }
async function open(id: string) { try {
    detail.value = await api.job(id);
}
catch (e: any) {
    ElMessage.error(e.message);
} }
async function action(kind: 'cancel' | 'retry', id: string) { try {
    kind === 'cancel' ? await api.cancelJob(id, actionKey()) : await api.retryJob(id, actionKey());
    ElMessage.success(kind === 'cancel' ? '已提交取消' : '已提交重试');
    load();
}
catch (e: any) {
    ElMessage.error(e.message);
} }
function openResolution(row: Job) { resolving.value = row; resolution.value = 'not_completed'; confirmed.value = false; notes.value = ''; resolveDialog.value = true; }
async function resolve() { if (!resolving.value)
    return; try {
    await api.resolveJob(resolving.value.id, { resolution: resolution.value, confirmed: true, notes: notes.value }, actionKey());
    resolveDialog.value = false;
    await load();
    ElMessage.success(resolution.value === 'not_completed' ? '已记录服务商未完成，本任务已可按服务端结果处理。' : '已提交取消确认');
}
catch (e: any) {
    ElMessage.error(e.message);
} }
onMounted(() => { load(); timer.value = window.setInterval(load, 5000); });
onBeforeUnmount(() => timer.value && clearInterval(timer.value));
</script>
<template>
<PageIntro title="任务中心" description="持续刷新队列状态。模型调用结果不明确时，不会直接重试或再次计费。">
  <el-button @click="load">刷新</el-button>
</PageIntro>
<el-select
  v-model="status"
  clearable
  placeholder="全部状态"
  @change="load"
>
  <el-option
    v-for="v in ['QUEUED','RUNNING','WAITING_USER','SUCCEEDED','FAILED','CANCELED']"
    :key="v"
    :label="statusLabel(v)"
    :value="v"
  />
</el-select>
<StatePanel
  :loading
  :error
  :empty="!jobs.length"
  empty-text="暂无任务"
  @retry="load"
>
  <el-table :data="jobs" class="data-table" @row-click="(row:Job)=>open(row.id)">
    <el-table-column label="任务" min-width="180">
      <template #default="{row}">{{ jobLabel(row.kind) }}</template>
    </el-table-column>
    <el-table-column label="状态" width="120">
      <template #default="{row}">
        <StatusTag :status="row.status"/>
      </template>
    </el-table-column>
    <el-table-column label="进度" min-width="180">
      <template #default="{row}">
        <el-progress
          :percentage="row.progress"
          :status="row.status==='FAILED'?'exception':row.status==='SUCCEEDED'?'success':''"
          :stroke-width="8"
        />
      </template>
    </el-table-column>
    <el-table-column prop="current_step" label="当前步骤" min-width="160" show-overflow-tooltip/>
    <el-table-column label="更新时间" width="170">
      <template #default="{row}">{{formatDate(row.updated_at)}}</template>
    </el-table-column>
    <el-table-column width="165">
      <template #default="{row}">
        <RouterLink
          v-if="row.status==='WAITING_USER' && isPublication(row) && !requiresProviderResolution(row)"
          to="/publications"
        >
          到发布中心处理
        </RouterLink>
        <el-button
          v-else-if="requiresProviderResolution(row)"
          link
          type="warning"
          @click.stop="openResolution(row)"
        >
          核验调用结果
        </el-button>
        <el-button
          v-if="['QUEUED','RUNNING','WAITING_USER'].includes(row.status)"
          link
          type="danger"
          @click.stop="action('cancel',row.id)"
        >
          取消
        </el-button>
        <el-button
          v-if="['FAILED','CANCELED'].includes(row.status)"
          link
          type="primary"
          @click.stop="action('retry',row.id)"
        >
          重试
        </el-button>
      </template>
    </el-table-column>
  </el-table>
</StatePanel>
<el-drawer
  :model-value="!!detail"
  title="任务详情"
  size="460px"
  @close="detail=null"
>
  <template v-if="detail">
    <div class="job-detail">
      <div>
        <StatusTag :status="detail.status"/>
        <b>{{jobLabel(detail.kind)}}</b>
      </div>
      <p v-if="detail.error_message" class="error-text">{{detail.error_code}}：{{detail.error_message}}</p>
      <el-alert
        v-if="requiresProviderResolution(detail)"
        type="warning"
        :closable="false"
        title="请先向服务商核验本次调用是否完成；在明确未完成前，重试可能导致再次计费。"
      />
      <el-button v-if="detail.blocking_job_id" link type="primary" @click="open(detail.blocking_job_id)">查看需要先处理的原任务</el-button>
      <el-descriptions :column="1" border size="small">
        <el-descriptions-item label="预计费用">{{costLabel(detail.cost?.estimated_usd)}}<span v-if="detail.cost?.estimated_usd == null" class="muted">（费用未知）</span></el-descriptions-item>
        <el-descriptions-item label="实际费用">{{costLabel(detail.cost?.actual_usd)}}<span v-if="detail.cost?.actual_usd == null" class="muted">（服务商账单为准）</span></el-descriptions-item>
        <el-descriptions-item label="费用状态">{{detail.cost?.status || 'UNKNOWN'}}</el-descriptions-item>
      </el-descriptions>
      <p class="muted">缺失用量不会被推算为 0 成本；时间线不展示服务商原始响应或敏感信息。</p>
      <el-timeline>
        <el-timeline-item
          v-for="(step,index) in displayTimeline(detail)"
          :key="`${step.at}-${step.kind}-${step.name}-${index}`"
          :timestamp="formatDate(step.at)"
          :type="step.status==='FAILED'?'danger':step.status==='SUCCEEDED'?'success':'primary'"
        >
          <b>{{step.name}}</b><el-tag size="small" effect="plain">{{step.kind}}</el-tag>
          <p class="muted">{{step.detail}}</p>
          <p v-if="step.usage && Object.keys(step.usage).length" class="muted">用量：{{Object.entries(step.usage).map(([key,value])=>`${key} ${value}`).join(' · ')}}</p>
        </el-timeline-item>
      </el-timeline>
    </div>
  </template>
</el-drawer>
<el-dialog
  v-model="resolveDialog"
  :title="resolving?.error_code==='RESTORE_REVIEW_REQUIRED'?'核验恢复任务':'核验模型调用结果'"
  width="520px"
>
  <el-alert
    type="warning"
    :closable="false"
    :title="resolving?.error_code==='RESTORE_REVIEW_REQUIRED'?'备份描述的是过去状态。请先核验服务商和发布平台，确认任务实际未完成后才能重试；取消不会撤回平台上的作品。':'网络超时不代表调用未发生。请先核验服务商记录；明确未完成后方可重试，可能再次计费。取消会永久禁止重发相同的未知请求。'"
  />
  <el-form label-position="top">
    <el-form-item label="核验结论">
      <el-radio-group v-model="resolution">
        <el-radio value="not_completed">已核验原任务未完成</el-radio>
        <el-radio value="cancel">取消本任务</el-radio>
      </el-radio-group>
    </el-form-item>
    <el-form-item label="核验说明">
      <el-input v-model="notes" type="textarea" :rows="3" placeholder="例如：已向服务商核实，响应 ID … 未创建任务"/>
    </el-form-item>
    <el-checkbox v-model="confirmed">我已核验并确认以上结论真实准确</el-checkbox>
  </el-form>
  <template #footer>
    <el-button @click="resolveDialog=false">返回</el-button>
    <el-button type="primary" :disabled="!confirmed || !notes" @click="resolve">提交核验结果</el-button>
  </template>
</el-dialog>
</template>
