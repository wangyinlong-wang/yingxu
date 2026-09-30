<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { api } from '../api';
import { actionKey, formatDate } from '../lib';
import { latestMetricChange, metricLabels, metricSeries, observedAtIso, orderedMetrics, type MetricField } from '../analytics';
import type { Account, AccountChannel, Metric, Publication } from '../types';
import PageIntro from '../components/PageIntro.vue';
import StatePanel from '../components/StatePanel.vue';
const analytics = ref<any>();
const publications = ref<Publication[]>([]);
const accounts = ref<Account[]>([]);
const selectedPublicationId = ref('');
const publicationMetrics = ref<Metric[]>([]);
const selectedField = ref<MetricField>('views');
const loading = ref(false), error = ref(''), dialog = ref(false);
const savingMetric = ref(false), collecting = ref(false), reporting = ref(false), reportPolling = ref(false);
const reportTimer = ref<number>();
const reportCountAtSubmit = ref(0);
const form = ref<any>({ publication_id: '', views: null, likes: null, comments: null, favorites: null, shares: null, observed_at: null as Date | null, notes: '' });
const published = computed(() => publications.value.filter(publication => publication.status === 'PUBLISHED'));
const selectedPublication = computed(() => publications.value.find(publication => publication.id === selectedPublicationId.value));
const channelOf = (value?: { channel?: AccountChannel } | null): AccountChannel => value?.channel === 'browser' ? 'browser' : 'android';
const collectChannel = computed(() => channelOf(
    accounts.value.find(account => account.id === selectedPublication.value?.account_id) ?? selectedPublication.value
));
const orderedPublicationMetrics = computed(() => orderedMetrics(publicationMetrics.value));
const series = computed(() => metricSeries(publicationMetrics.value, selectedField.value));
const latestChange = computed(() => latestMetricChange(publicationMetrics.value, selectedField.value));
const chartPoints = computed(() => {
    if (series.value.length < 2)
        return '';
    const values = series.value.map(point => point.value);
    const min = Math.min(...values), max = Math.max(...values), range = max - min || 1;
    return series.value.map((point, index) => `${index * 100 / (series.value.length - 1)},${90 - ((point.value - min) / range) * 80}`).join(' ');
});
async function load() {
    loading.value = true;
    error.value = '';
    try {
        const [analyticsResponse, publicationResponse, accountResponse] = await Promise.all([api.analytics(), api.publications(), api.accounts()]);
        analytics.value = analyticsResponse;
        publications.value = publicationResponse.items;
        accounts.value = accountResponse.items;
        if (selectedPublicationId.value)
            await loadPublicationMetrics();
    }
    catch (exception: any) {
        error.value = exception.message;
    }
    finally {
        loading.value = false;
    }
}
async function loadPublicationMetrics() {
    if (!selectedPublicationId.value) {
        publicationMetrics.value = [];
        return;
    }
    try {
        publicationMetrics.value = (await api.metrics(selectedPublicationId.value)).items;
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
}
async function save() {
    if (savingMetric.value)
        return;
    savingMetric.value = true;
    try {
        const payload: any = { ...form.value };
        for (const key of ['views', 'likes', 'comments', 'favorites', 'shares'])
            if (payload[key] === null)
                delete payload[key];
        payload.observed_at = observedAtIso(form.value.observed_at);
        if (!payload.observed_at)
            delete payload.observed_at;
        await api.createMetric(payload, actionKey());
        dialog.value = false;
        form.value = { publication_id: selectedPublicationId.value, views: null, likes: null, comments: null, favorites: null, shares: null, observed_at: null, notes: '' };
        await load();
        ElMessage.success('已保存手工数据');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        savingMetric.value = false;
    }
}
async function collectMetrics() {
    if (!selectedPublicationId.value || collecting.value)
        return;
    collecting.value = true;
    try {
        await api.collectMetrics(selectedPublicationId.value, actionKey());
        ElMessage.success(collectChannel.value === 'browser' ? '已提交浏览器读取任务，完成后会刷新该作品指标。' : '已提交手机读取任务，完成后会刷新该作品指标。');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        collecting.value = false;
    }
}
async function refreshReports() {
    try {
        const response = await api.analytics();
        analytics.value = response;
        if (reportPolling.value && (response.reports?.length ?? 0) > reportCountAtSubmit.value) {
            reportPolling.value = false;
            if (reportTimer.value)
                clearInterval(reportTimer.value);
            reportTimer.value = undefined;
        }
    }
    catch { /* retain the current report list during a polling failure */ }
}
async function report() {
    if (reporting.value)
        return;
    reporting.value = true;
    try {
        reportCountAtSubmit.value = analytics.value?.reports?.length ?? 0;
        await api.report(actionKey());
        reportPolling.value = true;
        reportTimer.value = window.setInterval(refreshReports, 5000);
        ElMessage.success('报告生成任务已提交，完成后会自动刷新。');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        reporting.value = false;
    }
}
function openManualDialog() { form.value = { publication_id: selectedPublicationId.value, views: null, likes: null, comments: null, favorites: null, shares: null, observed_at: null, notes: '' }; dialog.value = true; }
function changeText() { const { latest, previous, delta } = latestChange.value; if (latest === null)
    return '暂无可用快照'; if (previous === null || delta === null)
    return `最新 ${latest}；暂无上一条可比较快照`; return `最新 ${latest}；较上次 ${delta >= 0 ? '+' : ''}${delta}`; }
onMounted(load);
onBeforeUnmount(() => { if (reportTimer.value)
    clearInterval(reportTimer.value); });
</script>

<template>
<PageIntro title="数据复盘" description="保存真实指标快照并比较趋势；数值为单次观测，不会把累计快照相加。">
  <el-button @click="load">刷新</el-button>
  <el-button :disabled="!published.length" @click="openManualDialog">记录手工数据</el-button>
  <el-button type="primary" :loading="reporting" @click="report">生成复盘报告</el-button>
</PageIntro>
<StatePanel :loading :error @retry="load">
  <section class="metric-grid analytics-cards">
    <article class="metric">
      <span>已发布作品</span>
      <strong>{{published.length}}</strong>
    </article>
    <article class="metric">
      <span>已记录指标</span>
      <strong>{{analytics?.metrics?.length ?? 0}}</strong>
    </article>
    <article class="metric">
      <span>复盘报告</span>
      <strong>{{analytics?.reports?.length ?? 0}}</strong>
    </article>
  </section>
  <section class="analytics-workbench">
    <el-card shadow="never">
      <template #header>
        <div class="card-title">作品指标快照</div>
      </template>
      <el-form inline>
        <el-form-item label="已发布作品">
          <el-select
            v-model="selectedPublicationId"
            clearable
            placeholder="选择作品查看趋势"
            @change="loadPublicationMetrics"
          >
            <el-option
              v-for="publication in published"
              :key="publication.id"
              :value="publication.id"
              :label="`${publication.platform} · ${publication.content_title}`"
            />
          </el-select>
        </el-form-item>
        <el-button :loading="collecting" :disabled="!selectedPublicationId" @click="collectMetrics">{{collectChannel==='browser'?'从浏览器读取指标':'从手机读取指标'}}</el-button>
      </el-form>
      <el-empty v-if="!selectedPublicationId" description="选择一条已发布作品，查看每次真实观测。" :image-size="55"/>
      <template v-else>
        <div class="trend-toolbar">
          <el-radio-group v-model="selectedField">
            <el-radio-button v-for="(_,field) in metricLabels" :key="field" :value="field">
              {{metricLabels[field as MetricField]}}
            </el-radio-button>
          </el-radio-group>
          <span>{{changeText()}}</span>
        </div>
        <div v-if="series.length>=2" class="metric-chart">
          <svg
            viewBox="0 0 100 100"
            role="img"
            :aria-label="`${metricLabels[selectedField]}趋势`"
            preserveAspectRatio="none"
          >
            <polyline :points="chartPoints" fill="none" stroke="#315bff" stroke-width="2" vector-effect="non-scaling-stroke"/>
          </svg>
          <div>
            <span v-for="point in series" :key="point.at">{{formatDate(point.at)}}：{{point.value}}</span>
          </div>
        </div>
        <el-alert v-else type="info" :closable="false" title="至少需要两条非空快照才可比较趋势；空值与 0 会被分别保留。"/>
        <el-table :data="orderedPublicationMetrics" size="small" class="data-table">
          <el-table-column label="观测时间" width="170">
            <template #default="{row}">{{formatDate(row.observed_at)}}</template>
          </el-table-column>
          <el-table-column label="播放" width="82">
            <template #default="{row}">{{row.views===null?'—':row.views}}</template>
          </el-table-column>
          <el-table-column label="点赞" width="82">
            <template #default="{row}">{{row.likes===null?'—':row.likes}}</template>
          </el-table-column>
          <el-table-column label="评论" width="82">
            <template #default="{row}">{{row.comments===null?'—':row.comments}}</template>
          </el-table-column>
          <el-table-column label="收藏" width="82">
            <template #default="{row}">{{row.favorites===null?'—':row.favorites}}</template>
          </el-table-column>
          <el-table-column label="分享" width="82">
            <template #default="{row}">{{row.shares===null?'—':row.shares}}</template>
          </el-table-column>
          <el-table-column label="来源" width="85">
            <template #default="{row}">{{row.source==='manual'?'手工':row.source==='browser'?'浏览器':'手机'}}</template>
          </el-table-column>
          <el-table-column type="expand">
            <template #default="{row}">
              <pre class="metric-raw">{{JSON.stringify(row.raw,null,2)}}</pre>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </el-card>
    <el-card shadow="never">
      <template #header>复盘报告</template>
      <el-empty v-if="!analytics?.reports?.length" description="暂无报告" :image-size="60"/>
      <article v-for="report in analytics?.reports" :key="report.id" class="report">
        <div>
          <b>{{report.date}}</b>
          <small>{{formatDate(report.created_at)}}</small>
        </div>
        <pre>{{report.body}}</pre>
      </article>
    </el-card>
  </section>
</StatePanel>
<el-dialog v-model="dialog" title="记录手工指标" width="500px">
  <el-alert type="info" :closable="false" title="只可为已经确认发布的作品补录；未填写的指标会保留为空，0 会作为真实观测保存。"/>
  <el-form label-position="top">
    <el-form-item label="作品">
      <el-select v-model="form.publication_id">
        <el-option
          v-for="publication in published"
          :key="publication.id"
          :value="publication.id"
          :label="`${publication.platform} · ${publication.content_title}`"
        />
      </el-select>
    </el-form-item>
    <div class="form-grid">
      <el-form-item label="播放">
        <el-input-number v-model="form.views" :min="0"/>
      </el-form-item>
      <el-form-item label="点赞">
        <el-input-number v-model="form.likes" :min="0"/>
      </el-form-item>
      <el-form-item label="评论">
        <el-input-number v-model="form.comments" :min="0"/>
      </el-form-item>
      <el-form-item label="收藏">
        <el-input-number v-model="form.favorites" :min="0"/>
      </el-form-item>
      <el-form-item label="分享">
        <el-input-number v-model="form.shares" :min="0"/>
      </el-form-item>
    </div>
    <el-form-item label="观察时间（可选）">
      <el-date-picker v-model="form.observed_at" type="datetime"/>
    </el-form-item>
    <el-form-item label="备注">
      <el-input v-model="form.notes" type="textarea"/>
    </el-form-item>
  </el-form>
  <template #footer>
    <el-button :disabled="savingMetric" @click="dialog=false">取消</el-button>
    <el-button type="primary" :loading="savingMetric" :disabled="!form.publication_id" @click="save">保存</el-button>
  </template>
</el-dialog>
</template>
