<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';
import { api } from '../api';
import { actionKey, formatDate } from '../lib';
import type { Source, Topic } from '../types';
import StatePanel from '../components/StatePanel.vue';
import PageIntro from '../components/PageIntro.vue';
const router = useRouter();
const tab = ref<'sources' | 'topics'>('sources');
const sources = ref<Source[]>([]), topics = ref<Topic[]>([]);
const loading = ref(false), error = ref(''), topicQuery = ref(''), sourceQuery = ref('');
const sourceDialog = ref(false), editingSource = ref<Source | null>(null), savingSource = ref(false);
const collectingSource = ref<string | null>(null), changingSource = ref<string | null>(null), sourcePollTimer = ref<number>(), sourcePollAttempts = ref(0);
const sourceForm = ref<any>({ name: '', kind: 'rss', url: '', enabled: true });
const visibleSources = computed(() => { const query = sourceQuery.value.trim().toLowerCase(); return !query ? sources.value : sources.value.filter(source => `${source.name} ${source.url} ${source.kind}`.toLowerCase().includes(query)); });
const selectedTopics = computed(() => topics.value.filter(topic => !['DISMISSED', 'IGNORED'].includes(topic.status.toUpperCase())));
const isXSource = (kind:Source['kind']) => kind === 'x' || kind === 'x_tikhub';
const sourceKindLabel = (kind: Source['kind']) => kind === 'x_tikhub' ? 'X（TikHub）' : kind === 'x' ? 'X公开时间线' : kind === 'hackernews' ? 'Hacker News' : 'RSS / Atom';
async function load() {
    loading.value = true;
    error.value = '';
    try {
        const [sourceResponse, topicResponse] = await Promise.all([api.sources(), api.topics(topicQuery.value)]);
        sources.value = sourceResponse.items;
        topics.value = topicResponse.items;
    }
    catch (exception: any) {
        error.value = exception.message;
    }
    finally {
        loading.value = false;
    }
}
function openSource(source?: Source) { editingSource.value = source || null; sourceForm.value = source ? { name: source.name, kind: source.kind, url: source.url, enabled: source.enabled } : { name: '', kind: 'rss', url: '', enabled: true }; sourceDialog.value = true; }
async function saveSource() { if (savingSource.value)
    return; savingSource.value = true; try {
    if (editingSource.value)
        await api.patchSource(editingSource.value.id, { name: sourceForm.value.name, url: sourceForm.value.url, enabled: sourceForm.value.enabled, ...(isXSource(editingSource.value.kind) ? { kind: sourceForm.value.kind } : {}) });
    else
        await api.createSource(sourceForm.value);
    sourceDialog.value = false;
    await load();
    ElMessage.success(editingSource.value ? '来源已更新' : '来源已保存');
}
catch (exception: any) {
    ElMessage.error(exception.message);
}
finally {
    savingSource.value = false;
} }
async function toggleSource(source: Source, enabled: boolean) { if (changingSource.value)
    return; const previous = !enabled; changingSource.value = source.id; try {
    const updated = await api.patchSource(source.id, { enabled });
    const index = sources.value.findIndex(item => item.id === source.id);
    if (index >= 0)
        sources.value.splice(index, 1, updated);
}
catch (exception: any) {
    source.enabled = previous;
    ElMessage.error(exception.message);
}
finally {
    changingSource.value = null;
} }
async function collect(source: Source) { if (collectingSource.value)
    return; collectingSource.value = source.id;
    if (source.kind === 'x_tikhub') {
        try {
            await ElMessageBox.confirm('将通过 TikHub 读取该账号的一页帖子，成功请求按服务商规则计费并计入每日服务调用额度；不会自动翻页。', '确认 TikHub 采集', { confirmButtonText: '继续采集', cancelButtonText: '取消', type: 'warning' });
        }
        catch {
            collectingSource.value = null;
            return;
        }
    }
    sourcePollAttempts.value = 0; try {
    await api.collect(source.id, actionKey(), source.kind === 'x_tikhub' ? 'x_tikhub' : undefined);
    ElMessage.success('已提交采集任务，来源列表会安全刷新结果。');
    sourcePollTimer.value = window.setInterval(pollCollectedSource, 3000);
}
catch (exception: any) {
    collectingSource.value = null;
    if (exception?.body?.code === 'SOURCE_PROVIDER_CHANGED')
        await load();
    ElMessage.error(exception.message);
} }
async function pollCollectedSource() { if (!collectingSource.value)
    return; sourcePollAttempts.value++; try {
    const response = await api.sources();
    const before = sources.value.find(source => source.id === collectingSource.value);
    const refreshed = response.items.find(source => source.id === collectingSource.value);
    sources.value = response.items;
    if ((refreshed?.last_collected_at && refreshed.last_collected_at !== before?.last_collected_at) || refreshed?.last_error || sourcePollAttempts.value >= 20)
        stopCollectPolling();
}
catch {
    if (sourcePollAttempts.value >= 20)
        stopCollectPolling();
} }
function stopCollectPolling() { if (sourcePollTimer.value)
    clearInterval(sourcePollTimer.value); sourcePollTimer.value = undefined; collectingSource.value = null; }
async function createContent(topic: Topic) { try {
    const content: any = await api.createContent(topic.id);
    ElMessage.success('内容草稿已创建，正在打开工作台。');
    await router.push({ path: '/contents', query: { content_id: content.id } });
}
catch (exception: any) {
    ElMessage.error(exception.message);
} }
async function dismiss(topic: Topic) { try {
    await api.dismissTopic(topic.id, actionKey());
    topics.value = topics.value.map(item => item.id === topic.id ? { ...item, status: 'DISMISSED' } : item);
}
catch (exception: any) {
    ElMessage.error(exception.message);
} }
onMounted(load);
onBeforeUnmount(stopCollectPolling);
</script>

<template>
<PageIntro title="选题库" description="管理可信来源，筛选采集结果后创建可编辑的内容草稿。">
  <el-button @click="load">刷新</el-button>
  <el-button type="primary" @click="openSource()">添加来源</el-button>
</PageIntro>
<el-tabs v-model="tab" @tab-change="load">
  <el-tab-pane label="来源" name="sources"/>
  <el-tab-pane label="候选选题" name="topics"/>
</el-tabs>
<StatePanel
  :loading
  :error
  :empty="tab==='sources' ? !visibleSources.length : !selectedTopics.length"
  :empty-text="tab==='sources'?'还没有来源，添加 RSS、Hacker News、X公开时间线或 X（TikHub）来源开始采集。':'还没有候选选题，先执行来源采集。'"
  @retry="load"
>
  <template v-if="tab==='sources'">
    <el-input v-model="sourceQuery" clearable placeholder="搜索来源名称、地址或类型" class="source-search"/>
    <el-table :data="visibleSources" class="data-table">
      <el-table-column prop="name" label="来源" min-width="150"/>
      <el-table-column label="类型" width="120"><template #default="{row}">{{sourceKindLabel(row.kind)}}</template></el-table-column>
      <el-table-column prop="url" label="地址" min-width="230" show-overflow-tooltip/>
      <el-table-column label="状态" width="110">
        <template #default="{row}">
          <el-switch v-model="row.enabled" :loading="changingSource===row.id" @change="(enabled:boolean)=>toggleSource(row,enabled)"/>
        </template>
      </el-table-column>
      <el-table-column label="最近采集" width="165">
        <template #default="{row}">{{formatDate(row.last_collected_at)}}</template>
      </el-table-column>
      <el-table-column label="错误" min-width="170">
        <template #default="{row}">
          <span v-if="row.last_error" class="error-text">{{row.last_error}}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="145">
        <template #default="{row}">
          <el-button link type="primary" @click="openSource(row)">编辑</el-button>
          <el-button link type="primary" :loading="collectingSource===row.id" @click="collect(row)">采集</el-button>
        </template>
      </el-table-column>
    </el-table>
  </template>
  <div v-else class="topic-grid">
    <article v-for="topic in selectedTopics" :key="topic.id" class="topic-card">
      <div class="topic-score">
        {{Math.round(topic.relevance_score)}}
        <small>相关度</small>
      </div>
      <div class="topic-body">
        <a :href="topic.url" target="_blank" rel="noopener noreferrer">{{topic.title}}</a>
        <p>{{topic.summary}}</p>
        <small>{{topic.selection_reason || '尚未提供选择理由'}} · {{formatDate(topic.published_at)}}</small>
        <div>
          <el-button type="primary" size="small" @click="createContent(topic)">创建并打开工作台</el-button>
          <el-button size="small" @click="dismiss(topic)">忽略</el-button>
        </div>
      </div>
    </article>
  </div>
</StatePanel>
<el-dialog v-model="sourceDialog" :title="editingSource?'编辑内容来源':'添加内容来源'" width="480px">
  <el-form label-position="top">
    <el-form-item label="名称">
      <el-input v-model="sourceForm.name"/>
    </el-form-item>
    <el-form-item label="类型">
      <el-radio-group v-model="sourceForm.kind" :disabled="Boolean(editingSource && !isXSource(editingSource.kind))">
        <template v-if="editingSource && !isXSource(editingSource.kind)">
          <el-radio :value="editingSource.kind">{{sourceKindLabel(editingSource.kind)}}</el-radio>
        </template>
        <template v-else>
          <el-radio v-if="!editingSource" value="rss">RSS / Atom</el-radio>
          <el-radio v-if="!editingSource" value="hackernews">Hacker News</el-radio>
          <el-radio value="x">X公开时间线</el-radio>
          <el-radio value="x_tikhub">X（TikHub）</el-radio>
        </template>
      </el-radio-group>
    </el-form-item>
    <el-form-item label="地址">
      <el-input v-model="sourceForm.url" :placeholder="isXSource(sourceForm.kind)?'https://x.com/OpenAI':'https://…'"/>
      <p v-if="!editingSource && sourceForm.kind==='x'" class="form-help">填写账号主页；采集近 30 天公开可见的技术/AI 帖子并保留原帖链接。公开时间线不是完整历史。</p>
      <p v-if="!editingSource && sourceForm.kind==='x_tikhub'" class="form-help">填写账号主页。本机 TikHub 密钥已配置后可采集；每次只读取一页公开帖子，不会自动翻页。</p>
    </el-form-item>
    <el-form-item>
      <el-checkbox v-model="sourceForm.enabled">保存后启用采集</el-checkbox>
    </el-form-item>
  </el-form>
  <template #footer>
    <el-button :disabled="savingSource" @click="sourceDialog=false">取消</el-button>
    <el-button
      type="primary"
      :loading="savingSource"
      :disabled="!sourceForm.name || !sourceForm.url"
      @click="saveSource"
    >
      保存
    </el-button>
  </template>
</el-dialog>
</template>
