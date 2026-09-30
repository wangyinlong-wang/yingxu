<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';
import { api } from '../api';
import { actionKey, formatDate, isPaidSource, isXProfileSource, parseKeywords, sourceHealth, sourceKindLabel, xSearchParts, xSearchUrl } from '../lib';
import type { Source, SourceKind, SourcePreset, Topic } from '../types';
import StatePanel from '../components/StatePanel.vue';
import PageIntro from '../components/PageIntro.vue';
const router = useRouter();
const tab = ref<'sources' | 'topics'>('sources');
const sources = ref<Source[]>([]), topics = ref<Topic[]>([]);
const loading = ref(false), error = ref(''), topicQuery = ref(''), sourceQuery = ref(''), topicSort = ref<'heat' | 'relevance'>('heat'), topicDays = ref<number>(2);
const sourceDialog = ref(false), editingSource = ref<Source | null>(null), savingSource = ref(false);
const collectingSource = ref<string | null>(null), changingSource = ref<string | null>(null), sourcePollTimer = ref<number>(), sourcePollAttempts = ref(0);
type SourceForm = { name: string; kind: SourceKind; url: string; enabled: boolean; keywords: string; path_prefix: string; search_query: string; search_mode: 'live' | 'top' };
const blankForm = (): SourceForm => ({ name: '', kind: 'rss', url: '', enabled: true, keywords: '', path_prefix: '', search_query: '', search_mode: 'live' });
const sourceForm = ref<SourceForm>(blankForm());
const importDialog = ref(false), importingTopic = ref(false);
const importForm = ref({ source_id: '', title: '', url: '', summary: '' });
const presetDialog = ref(false), presets = ref<SourcePreset[]>([]), selectedPresets = ref<string[]>([]), applyingPresets = ref(false), loadingPresets = ref(false);
const canImport = computed(() => Object.values(importForm.value).every(value => value.trim()));
const visibleSources = computed(() => { const query = sourceQuery.value.trim().toLowerCase(); return !query ? sources.value : sources.value.filter(source => `${source.name} ${source.url} ${source.kind} ${(source.keywords || []).join(' ')}`.toLowerCase().includes(query)); });
const selectedTopics = computed(() => topics.value.filter(topic => !['DISMISSED', 'IGNORED'].includes(topic.status.toUpperCase())));
const canSaveSource = computed(() => Boolean(sourceForm.value.name && (sourceForm.value.kind === 'x_tikhub_search' ? sourceForm.value.search_query.trim() : sourceForm.value.url)));
async function load() {
    loading.value = true;
    error.value = '';
    try {
        const [sourceResponse, topicResponse] = await Promise.all([api.sources(), api.topics(topicQuery.value, topicSort.value, topicDays.value || undefined)]);
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
function openSource(source?: Source) {
    editingSource.value = source || null;
    if (!source) sourceForm.value = blankForm();
    else {
        const search = source.kind === 'x_tikhub_search' ? xSearchParts(source.url) : { query: '', mode: 'live' as const };
        sourceForm.value = { name: source.name, kind: source.kind, url: source.url, enabled: source.enabled, keywords: (source.keywords || []).join('，'), path_prefix: source.options?.path_prefix || '', search_query: search.query, search_mode: search.mode };
    }
    sourceDialog.value = true;
}
function sourcePayload() {
    const form = sourceForm.value;
    const url = form.kind === 'x_tikhub_search' ? xSearchUrl(form.search_query, form.search_mode) : form.url;
    return { name: form.name, url, enabled: form.enabled, keywords: parseKeywords(form.keywords), ...(form.kind === 'html_list' ? { options: { path_prefix: form.path_prefix.trim() || null } } : {}) };
}
async function importTopic() {
    if (importingTopic.value || !canImport.value) return;
    importingTopic.value = true;
    try {
        const result = await api.importTopic(importForm.value);
        importDialog.value = false;
        tab.value = 'topics';
        await load();
        if (!result.created) ElMessage.info('该原帖已有选题，将打开已有内容，不覆盖原文。');
        await createContent(result.topic);
        importForm.value = { source_id: '', title: '', url: '', summary: '' };
    } catch (exception: any) {
        ElMessage.error(exception.message);
    } finally {
        importingTopic.value = false;
    }
}
async function saveSource() { if (savingSource.value)
    return; savingSource.value = true; try {
    if (editingSource.value)
        await api.patchSource(editingSource.value.id, { ...sourcePayload(), ...(isXProfileSource(editingSource.value.kind) ? { kind: sourceForm.value.kind } : {}) });
    else
        await api.createSource({ ...sourcePayload(), kind: sourceForm.value.kind });
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
    if (isPaidSource(source.kind)) {
        const target = source.kind === 'x_tikhub_search' ? '该搜索条件的一页结果' : '该账号的一页帖子';
        try {
            await ElMessageBox.confirm(`将通过 TikHub 读取${target}，成功请求按服务商规则计费并计入每日服务调用额度；不会自动翻页。`, '确认 TikHub 采集', { confirmButtonText: '继续采集', cancelButtonText: '取消', type: 'warning' });
        }
        catch {
            collectingSource.value = null;
            return;
        }
    }
    sourcePollAttempts.value = 0; try {
    await api.collect(source.id, actionKey(), isPaidSource(source.kind) ? source.kind : undefined);
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
    if ((refreshed?.last_collected_at && refreshed.last_collected_at !== before?.last_collected_at) || (refreshed && refreshed.consecutive_failures !== before?.consecutive_failures) || sourcePollAttempts.value >= 20)
        stopCollectPolling();
}
catch {
    if (sourcePollAttempts.value >= 20)
        stopCollectPolling();
} }
function stopCollectPolling() { if (sourcePollTimer.value)
    clearInterval(sourcePollTimer.value); sourcePollTimer.value = undefined; collectingSource.value = null; }
async function openPresets() {
    presetDialog.value = true;
    loadingPresets.value = true;
    try {
        presets.value = (await api.sourcePresets()).items;
        selectedPresets.value = presets.value.filter(item => !item.added && !item.paid).map(item => item.id);
    } catch (exception: any) {
        ElMessage.error(exception.message);
    } finally {
        loadingPresets.value = false;
    }
}
async function applyPresets() {
    if (applyingPresets.value || !selectedPresets.value.length) return;
    applyingPresets.value = true;
    try {
        const result = await api.applySourcePresets(selectedPresets.value);
        presetDialog.value = false;
        await load();
        ElMessage.success(`已添加 ${result.items.length} 个来源${result.skipped ? `，${result.skipped} 个已存在` : ''}`);
    } catch (exception: any) {
        ElMessage.error(exception.message);
    } finally {
        applyingPresets.value = false;
    }
}
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
  <el-button :disabled="!sources.length" @click="importDialog=true">粘贴原帖</el-button>
  <el-button @click="openPresets">推荐来源</el-button>
  <el-button type="primary" @click="openSource()">添加来源</el-button>
</PageIntro>
<el-tabs v-model="tab" @tab-change="load">
  <el-tab-pane label="来源" name="sources"/>
  <el-tab-pane label="候选选题" name="topics"/>
</el-tabs>
<div v-if="tab==='topics'" class="topic-toolbar">
  <el-radio-group v-model="topicSort" size="small" @change="load">
    <el-radio-button value="heat">按热度</el-radio-button>
    <el-radio-button value="relevance">按相关度</el-radio-button>
  </el-radio-group>
  <el-radio-group v-model="topicDays" size="small" @change="load">
    <el-radio-button :value="2">最近 2 天</el-radio-button>
    <el-radio-button :value="7">最近 7 天</el-radio-button>
    <el-radio-button :value="0">全部</el-radio-button>
  </el-radio-group>
  <small class="muted">按原文发布时间筛选，没有发布时间的条目只在「全部」中显示；热度综合相关度、发布时间、互动量和多来源印证。</small>
</div>
<StatePanel
  :loading
  :error
  :empty="tab==='sources' ? !visibleSources.length : !selectedTopics.length"
  :empty-text="tab==='sources'?'还没有来源，点击「推荐来源」一键添加 OpenAI、Anthropic 等官方资讯，或手动添加 RSS、网页列表、X 来源。':topicDays?`最近 ${topicDays} 天没有新发布的候选选题，可切换到更长时间范围或执行来源采集。`:'还没有候选选题，先执行来源采集。'"
  @retry="load"
>
  <template v-if="tab==='sources'">
    <el-input v-model="sourceQuery" clearable placeholder="搜索来源名称、地址、类型或关注词" class="source-search"/>
    <el-table :data="visibleSources" class="data-table">
      <el-table-column label="来源" min-width="170">
        <template #default="{row}">
          <div>{{row.name}}</div>
          <div v-if="row.keywords?.length" class="source-keywords">
            <el-tag v-for="keyword in row.keywords" :key="keyword" size="small" effect="plain">{{keyword}}</el-tag>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="130"><template #default="{row}">{{sourceKindLabel(row.kind)}}</template></el-table-column>
      <el-table-column prop="url" label="地址" min-width="220" show-overflow-tooltip/>
      <el-table-column label="状态" width="90">
        <template #default="{row}">
          <el-switch v-model="row.enabled" :loading="changingSource===row.id" @change="(enabled:boolean)=>toggleSource(row,enabled)"/>
        </template>
      </el-table-column>
      <el-table-column label="健康" width="170">
        <template #default="{row}">
          <el-tag :type="sourceHealth(row).type" size="small">{{sourceHealth(row).label}}</el-tag>
          <small class="muted source-health-meta">最近成功 {{formatDate(row.last_success_at)}}</small>
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
  <template v-else>
    <div class="topic-grid">
      <article v-for="topic in selectedTopics" :key="topic.id" class="topic-card">
        <div class="topic-score">
          {{Math.round(topicSort==='heat' && topic.heat_score != null ? topic.heat_score : topic.relevance_score)}}
          <small>{{topicSort==='heat' && topic.heat_score != null ? '热度' : '相关度'}}</small>
        </div>
        <div class="topic-body">
          <a :href="topic.url" target="_blank" rel="noopener noreferrer">{{topic.title}}</a>
          <p>{{topic.summary}}</p>
          <small>{{topic.selection_reason || '尚未提供选择理由'}} · 相关度 {{Math.round(topic.relevance_score)}} · {{formatDate(topic.published_at)}}</small>
          <div v-if="(topic.cluster_size || 1) > 1" class="topic-related">
            <el-tag size="small" type="success">{{topic.cluster_size}} 个来源报道</el-tag>
            <a v-for="item in topic.related" :key="item.url" :href="item.url" target="_blank" rel="noopener noreferrer">{{item.source_name}}</a>
          </div>
          <div>
            <el-button type="primary" size="small" @click="createContent(topic)">创建并打开工作台</el-button>
            <el-button size="small" @click="dismiss(topic)">忽略</el-button>
          </div>
        </div>
      </article>
    </div>
  </template>
</StatePanel>
<el-dialog v-model="sourceDialog" :title="editingSource?'编辑内容来源':'添加内容来源'" width="min(520px, 92vw)">
  <el-form label-position="top">
    <el-form-item label="名称">
      <el-input v-model="sourceForm.name"/>
    </el-form-item>
    <el-form-item label="类型">
      <el-radio-group v-model="sourceForm.kind" :disabled="Boolean(editingSource && !isXProfileSource(editingSource.kind))">
        <template v-if="editingSource && !isXProfileSource(editingSource.kind)">
          <el-radio :value="editingSource.kind">{{sourceKindLabel(editingSource.kind)}}</el-radio>
        </template>
        <template v-else>
          <el-radio v-if="!editingSource" value="rss">RSS / Atom</el-radio>
          <el-radio v-if="!editingSource" value="html_list">网页列表</el-radio>
          <el-radio v-if="!editingSource" value="hackernews">Hacker News</el-radio>
          <el-radio value="x">X公开时间线</el-radio>
          <el-radio value="x_tikhub">X（TikHub）</el-radio>
          <el-radio v-if="!editingSource" value="x_tikhub_search">X搜索（TikHub）</el-radio>
        </template>
      </el-radio-group>
    </el-form-item>
    <template v-if="sourceForm.kind==='x_tikhub_search'">
      <el-form-item label="搜索条件">
        <el-input v-model="sourceForm.search_query" type="textarea" :rows="2" maxlength="500" placeholder="from:xai OR from:grok -filter:replies"/>
        <p class="form-help">支持 X 高级搜索语法：from:账号、OR、min_faves:1000、-filter:replies。每次采集读取一页结果，按次计费并计入每日调用额度。</p>
      </el-form-item>
      <el-form-item label="排序">
        <el-radio-group v-model="sourceForm.search_mode">
          <el-radio value="live">最新</el-radio>
          <el-radio value="top">热门</el-radio>
        </el-radio-group>
      </el-form-item>
    </template>
    <el-form-item v-else label="地址">
      <el-input v-model="sourceForm.url" :placeholder="isXProfileSource(sourceForm.kind)?'https://x.com/OpenAI':sourceForm.kind==='html_list'?'https://www.anthropic.com/news':'https://…'"/>
      <p v-if="!editingSource && sourceForm.kind==='x'" class="form-help">填写账号主页；采集近 30 天公开可见的技术/AI 帖子并保留原帖链接。公开时间线常被 X 限流（HTTP 429），稳定采集建议改用 TikHub。</p>
      <p v-if="!editingSource && sourceForm.kind==='x_tikhub'" class="form-help">填写账号主页。本机 TikHub 密钥已配置后可采集；每次只读取一页公开帖子，不会自动翻页。</p>
      <p v-if="sourceForm.kind==='html_list'" class="form-help">适合没有 RSS 的官方新闻页：只读取该页面上同站点、指定路径下的文章链接，不会递归抓取。</p>
    </el-form-item>
    <el-form-item v-if="sourceForm.kind==='html_list'" label="文章路径前缀（可选）">
      <el-input v-model="sourceForm.path_prefix" placeholder="默认使用列表页路径，例如 /news/"/>
    </el-form-item>
    <el-form-item label="关注词（可选）">
      <el-input v-model="sourceForm.keywords" placeholder="用逗号分隔，例如：Grok，Claude Code，Sora"/>
      <p class="form-help">命中关注词的内容会提高相关度；未命中内置 AI 方向但命中关注词的帖子也会保留。</p>
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
      :disabled="!canSaveSource"
      @click="saveSource"
    >
      保存
    </el-button>
  </template>
</el-dialog>
<el-dialog v-model="presetDialog" title="推荐来源" width="min(620px, 92vw)">
  <p class="form-help">官方资讯源免费读取；标记「按次计费」的来源通过 TikHub 读取 X，只在你手动采集或在定时计划中明确包含付费来源时调用。</p>
  <div v-loading="loadingPresets" class="preset-list">
    <el-checkbox-group v-model="selectedPresets">
      <el-checkbox v-for="preset in presets" :key="preset.id" :value="preset.id" :disabled="preset.added" class="preset-item">
        <span class="preset-body">
          <b>{{preset.name}}</b>
          <el-tag size="small" effect="plain">{{sourceKindLabel(preset.kind)}}</el-tag>
          <el-tag v-if="preset.paid" size="small" type="warning">按次计费</el-tag>
          <el-tag v-if="preset.added" size="small" type="info">已添加</el-tag>
          <small class="muted">{{preset.description}}</small>
        </span>
      </el-checkbox>
    </el-checkbox-group>
  </div>
  <template #footer>
    <el-button :disabled="applyingPresets" @click="presetDialog=false">取消</el-button>
    <el-button type="primary" :loading="applyingPresets" :disabled="!selectedPresets.length" @click="applyPresets">添加所选来源</el-button>
  </template>
</el-dialog>
<el-dialog v-model="importDialog" title="粘贴原帖创建选题" width="min(600px, 92vw)" :close-on-click-modal="!importingTopic" :close-on-press-escape="!importingTopic" :show-close="!importingTopic">
  <p class="form-help">保存你提供的原帖正文，不访问来源网站或调用模型。导入后仍需复核事实、预览视频和审核发布。</p>
  <el-form label-position="top" :disabled="importingTopic" @submit.prevent="importTopic">
    <el-form-item label="归属来源">
      <el-select v-model="importForm.source_id" placeholder="选择原帖所属来源" style="width:100%">
        <el-option v-for="source in sources" :key="source.id" :label="source.name" :value="source.id"/>
      </el-select>
    </el-form-item>
    <el-form-item label="原帖链接"><el-input v-model="importForm.url" maxlength="2000" placeholder="https://x.com/账号/status/原帖编号"/></el-form-item>
    <el-form-item label="选题标题"><el-input v-model="importForm.title" maxlength="200" show-word-limit/></el-form-item>
    <el-form-item label="原帖正文"><el-input v-model="importForm.summary" type="textarea" :rows="7" maxlength="12000" show-word-limit/></el-form-item>
  </el-form>
  <template #footer>
    <el-button :disabled="importingTopic" @click="importDialog=false">取消</el-button>
    <el-button type="primary" :loading="importingTopic" :disabled="!canImport" @click="importTopic">导入并打开工作台</el-button>
  </template>
</el-dialog>
</template>

<style scoped>
.source-keywords{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.source-health-meta{display:block;margin-top:4px;font-size:11px}
.topic-toolbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.topic-related{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:6px 0;font-size:12px}
.preset-list{max-height:60vh;overflow:auto}
.preset-list :deep(.el-checkbox-group){display:flex;flex-direction:column}
.preset-item{display:flex;align-items:flex-start;height:auto;margin-right:0;padding:10px 0;border-bottom:1px solid var(--line)}
.preset-item :deep(.el-checkbox__input){margin-top:3px}
.preset-item :deep(.el-checkbox__label){white-space:normal;font-size:14px;line-height:1.5}
.preset-body{display:flex;flex-wrap:wrap;align-items:center;gap:6px;min-width:0}
.preset-body small{flex-basis:100%}
</style>
