<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { formatDate } from '../lib'
import type { Benchmark, BenchmarkInput, DemandSignal, DemandSignalInput, Experiment, ExperimentInput } from '../types'
import PageIntro from '../components/PageIntro.vue'
import StatePanel from '../components/StatePanel.vue'
import StatusTag from '../components/StatusTag.vue'

type DialogKind = 'demand'|'benchmark'|'experiment'
const tab = ref<DialogKind>('demand')
const loading = ref(false), error = ref(''), saving = ref(false), dialog = ref(false), editingId = ref<string | null>(null)
const demandStatus = ref(''), experimentStatus = ref('')
const signals = ref<DemandSignal[]>([]), benchmarks = ref<Benchmark[]>([]), experiments = ref<Experiment[]>([])
const contents = ref<{id:string;title:string}[]>([]), publications = ref<{id:string;content_title:string;platform:string}[]>([])
const scoreNames:Record<keyof DemandSignal['scores'],string> = {demand:'需求',production:'生产',delivery:'交付',presentation:'呈现',compliance:'合规',iteration:'迭代'}
const scoreEntries = Object.entries(scoreNames) as [keyof DemandSignal['scores'], string][]
const emptySignal = ():DemandSignalInput => ({ title:'', source_kind:'manual', source_url:null, evidence:'', scores:{demand:0,production:0,delivery:0,presentation:0,compliance:0,iteration:0}, status:'CANDIDATE' })
const emptyBenchmark = ():BenchmarkInput => ({ platform:'douyin', account_name:'', content_url:null, title:'', content_type:'video', analysis:{audience:'',pain_point:'',hook:'',cover:'',structure:'',cta:'',metrics:'',learnings:'',prohibited_copy:''} })
const emptyExperiment = ():ExperimentInput => ({ name:'', hypothesis:'', variable:'angle', status:'DRAFT', content_ids:[], publication_ids:[], observation_windows:[], conclusion:'', next_action:'undecided' })
const signalForm = ref(emptySignal()), benchmarkForm = ref(emptyBenchmark()), experimentForm = ref(emptyExperiment())
const dialogTitle = computed(() => `${editingId.value ? '编辑' : '新建'}${{demand:'需求信号',benchmark:'对标拆解',experiment:'内容实验'}[tab.value]}`)

function errorMessage(value:unknown) { return value instanceof Error ? value.message : '请求失败，请稍后重试' }
function experimentMetrics(item:Experiment):string {
  const values=item.publication_metrics || []
  if (!values.length) return '暂无指标'
  return values.map((entry,index)=>{
    const latest=entry.latest_metric
    if (!latest) return `作品 ${index + 1}：尚无观测`
    const delta=entry.delta.views == null ? '' : `，播放增量 ${entry.delta.views}`
    return `作品 ${index + 1}：播放 ${latest.views ?? '—'}、赞 ${latest.likes ?? '—'}${delta}`
  }).join('；')
}
function signalInput(item:DemandSignal):DemandSignalInput { return { title:item.title, source_kind:item.source_kind, source_url:item.source_url, evidence:item.evidence, scores:{...item.scores}, status:item.status } }
function benchmarkInput(item:Benchmark):BenchmarkInput { return { platform:item.platform, account_name:item.account_name, content_url:item.content_url, title:item.title, content_type:item.content_type, analysis:{...item.analysis} } }
function experimentInput(item:Experiment):ExperimentInput { return { name:item.name, hypothesis:item.hypothesis, variable:item.variable, status:item.status, content_ids:[...item.content_ids], publication_ids:[...item.publication_ids], observation_windows:[...item.observation_windows], conclusion:item.conclusion, next_action:item.next_action } }
async function load() {
  loading.value = true; error.value = ''
  try {
    const [signalResult, benchmarkResult, experimentResult, contentResult, publicationResult] = await Promise.all([
      api.strategyDemandSignals(demandStatus.value), api.strategyBenchmarks(), api.strategyExperiments(experimentStatus.value), api.contents('', ''), api.publications()
    ])
    signals.value = signalResult.items; benchmarks.value = benchmarkResult.items; experiments.value = experimentResult.items
    contents.value = contentResult.items.map(item => ({id:item.id,title:item.title})); publications.value = publicationResult.items
  } catch (exception) { error.value = errorMessage(exception) } finally { loading.value = false }
}
function open(kind:DialogKind, item?:DemandSignal|Benchmark|Experiment) {
  tab.value = kind; editingId.value = item?.id ?? null
  if (kind === 'demand') signalForm.value = item ? signalInput(item as DemandSignal) : emptySignal()
  if (kind === 'benchmark') benchmarkForm.value = item ? benchmarkInput(item as Benchmark) : emptyBenchmark()
  if (kind === 'experiment') experimentForm.value = item ? experimentInput(item as Experiment) : emptyExperiment()
  dialog.value = true
}
async function save() {
  if (saving.value) return
  const form = tab.value === 'demand' ? signalForm.value : tab.value === 'benchmark' ? benchmarkForm.value : experimentForm.value
  const label = {demand:'需求信号',benchmark:'对标拆解',experiment:'内容实验'}[tab.value]
  const name = tab.value === 'experiment' ? experimentForm.value.name : tab.value === 'benchmark' ? benchmarkForm.value.title : signalForm.value.title
  if (!name.trim()) { ElMessage.error(`${label}名称不能为空`); return }
  saving.value = true
  try {
    if (tab.value === 'demand') editingId.value ? await api.patchDemandSignal(editingId.value, signalForm.value) : await api.createDemandSignal(signalForm.value)
    if (tab.value === 'benchmark') editingId.value ? await api.patchBenchmark(editingId.value, benchmarkForm.value) : await api.createBenchmark(benchmarkForm.value)
    if (tab.value === 'experiment') editingId.value ? await api.patchExperiment(editingId.value, experimentForm.value) : await api.createExperiment(experimentForm.value)
    dialog.value = false; ElMessage.success(`${label}已保存`); await load()
  } catch (exception) { ElMessage.error(errorMessage(exception)) } finally { saving.value = false }
}
onMounted(load)
</script>

<template>
  <PageIntro title="策略与实验" description="将公开需求线索、对标观察与内容实验留在本地；链接仅供人工查看，不会自动抓取。">
    <el-button @click="load">刷新</el-button>
  </PageIntro>
  <el-tabs v-model="tab" class="strategy-tabs">
    <el-tab-pane label="需求信号" name="demand">
      <div class="tab-actions"><el-select v-model="demandStatus" clearable placeholder="全部状态" @change="load"><el-option v-for="value in ['CANDIDATE','VALIDATED','REJECTED']" :key="value" :value="value" :label="value" /></el-select><el-button type="primary" @click="open('demand')">新建需求信号</el-button></div>
      <StatePanel :loading :error :empty="!signals.length" empty-text="暂无需求信号。记录公开线索或人工观察后再做判断。" @retry="load">
        <el-table :data="signals" class="data-table"><el-table-column prop="title" label="标题" min-width="190" /><el-table-column label="来源" width="110"><template #default="{row}">{{row.source_kind}}</template></el-table-column><el-table-column label="六维评分" min-width="260"><template #default="{row}"><div class="score-row"><span v-for="[key,name] in scoreEntries" :key="key">{{name}} {{row.scores[key]}}/5</span></div></template></el-table-column><el-table-column label="状态" width="120"><template #default="{row}"><StatusTag :status="row.status" /></template></el-table-column><el-table-column label="更新时间" width="150"><template #default="{row}">{{formatDate(row.updated_at)}}</template></el-table-column><el-table-column width="90"><template #default="{row}"><el-button link type="primary" @click="open('demand',row)">编辑</el-button></template></el-table-column></el-table>
      </StatePanel>
    </el-tab-pane>
    <el-tab-pane label="对标拆解" name="benchmark">
      <div class="tab-actions"><span class="muted">只记录人工观察，不复制内容或自动访问外链。</span><el-button type="primary" @click="open('benchmark')">新建对标拆解</el-button></div>
      <StatePanel :loading :error :empty="!benchmarks.length" empty-text="暂无对标记录。可录入公开作品的观察与不可照搬边界。" @retry="load">
        <el-table :data="benchmarks" class="data-table"><el-table-column prop="account_name" label="账号" min-width="140" /><el-table-column prop="title" label="内容标题" min-width="180" /><el-table-column label="链接" min-width="110"><template #default="{row}"><a v-if="row.content_url" :href="row.content_url" target="_blank" rel="noopener noreferrer">打开公开链接</a><span v-else>—</span></template></el-table-column><el-table-column label="可借鉴点" min-width="190" show-overflow-tooltip><template #default="{row}">{{row.analysis.learnings}}</template></el-table-column><el-table-column width="90"><template #default="{row}"><el-button link type="primary" @click="open('benchmark',row)">编辑</el-button></template></el-table-column></el-table>
      </StatePanel>
    </el-tab-pane>
    <el-tab-pane label="内容实验" name="experiment">
      <div class="tab-actions"><el-select v-model="experimentStatus" clearable placeholder="全部状态" @change="load"><el-option v-for="value in ['DRAFT','RUNNING','COMPLETED','CANCELED']" :key="value" :value="value" :label="value" /></el-select><el-button type="primary" @click="open('experiment')">新建内容实验</el-button></div>
      <StatePanel :loading :error :empty="!experiments.length" empty-text="暂无内容实验。将假设、观察窗口与下一步动作关联到内容和发布记录。" @retry="load">
        <el-table :data="experiments" class="data-table"><el-table-column prop="name" label="实验" min-width="160" /><el-table-column prop="hypothesis" label="假设" min-width="210" show-overflow-tooltip /><el-table-column label="状态" width="120"><template #default="{row}"><StatusTag :status="row.status" /></template></el-table-column><el-table-column label="最新指标" min-width="240"><template #default="{row}"><span>{{experimentMetrics(row)}}</span></template></el-table-column><el-table-column prop="next_action" label="下一步" width="130" /><el-table-column width="90"><template #default="{row}"><el-button link type="primary" @click="open('experiment',row)">编辑</el-button></template></el-table-column></el-table>
      </StatePanel>
    </el-tab-pane>
  </el-tabs>

  <el-dialog v-model="dialog" :title="dialogTitle" width="min(720px, calc(100% - 32px))" destroy-on-close>
    <el-form v-if="tab==='demand'" label-position="top">
      <el-form-item label="标题" required><el-input v-model="signalForm.title" maxlength="200" /></el-form-item>
      <div class="form-grid"><el-form-item label="线索来源"><el-select v-model="signalForm.source_kind"><el-option v-for="value in ['search','comment','forum','customer','manual']" :key="value" :value="value" /></el-select></el-form-item><el-form-item label="状态"><el-select v-model="signalForm.status"><el-option v-for="value in ['CANDIDATE','VALIDATED','REJECTED']" :key="value" :value="value" /></el-select></el-form-item><el-form-item label="公开来源链接"><el-input v-model="signalForm.source_url" placeholder="https://（可选）" /></el-form-item></div>
      <el-form-item label="证据 / 摘要"><el-input v-model="signalForm.evidence" type="textarea" :rows="3" /></el-form-item><div class="score-fields"><el-form-item v-for="[key,name] in scoreEntries" :key="key" :label="`${name}评分`"><el-input-number v-model="signalForm.scores[key]" :min="0" :max="5" /></el-form-item></div>
    </el-form>
    <el-form v-else-if="tab==='benchmark'" label-position="top"><div class="form-grid"><el-form-item label="平台"><el-select v-model="benchmarkForm.platform"><el-option v-for="value in ['douyin','xiaohongshu','other']" :key="value" :value="value" /></el-select></el-form-item><el-form-item label="账号" required><el-input v-model="benchmarkForm.account_name" /></el-form-item><el-form-item label="内容类型"><el-select v-model="benchmarkForm.content_type"><el-option v-for="value in ['video','image_note','other']" :key="value" :value="value" /></el-select></el-form-item></div><el-form-item label="内容标题" required><el-input v-model="benchmarkForm.title" /></el-form-item><el-form-item label="公开作品链接"><el-input v-model="benchmarkForm.content_url" placeholder="https://（可选）" /></el-form-item><div class="form-grid"><el-form-item v-for="(label,key) in {audience:'受众',pain_point:'痛点',hook:'钩子',cover:'封面',structure:'结构',cta:'CTA',metrics:'指标',learnings:'可借鉴点',prohibited_copy:'禁止照搬'}" :key="key" :label="label"><el-input v-model="benchmarkForm.analysis[key]" type="textarea" :rows="2" /></el-form-item></div></el-form>
    <el-form v-else label-position="top"><el-form-item label="实验名称" required><el-input v-model="experimentForm.name" /></el-form-item><el-form-item label="假设"><el-input v-model="experimentForm.hypothesis" type="textarea" :rows="2" /></el-form-item><div class="form-grid"><el-form-item label="变量"><el-select v-model="experimentForm.variable"><el-option v-for="value in ['angle','title','cover','format','publish_time','other']" :key="value" :value="value" /></el-select></el-form-item><el-form-item label="状态"><el-select v-model="experimentForm.status"><el-option v-for="value in ['DRAFT','RUNNING','COMPLETED','CANCELED']" :key="value" :value="value" /></el-select></el-form-item><el-form-item label="下一步"><el-select v-model="experimentForm.next_action"><el-option v-for="value in ['continue','revise_title','revise_cover','change_angle','stop','undecided']" :key="value" :value="value" /></el-select></el-form-item></div><el-form-item label="关联内容"><el-select v-model="experimentForm.content_ids" multiple clearable><el-option v-for="item in contents" :key="item.id" :value="item.id" :label="item.title" /></el-select></el-form-item><el-form-item label="关联发布记录"><el-select v-model="experimentForm.publication_ids" multiple clearable><el-option v-for="item in publications" :key="item.id" :value="item.id" :label="`${item.content_title} · ${item.platform}`" /></el-select></el-form-item><el-form-item label="观察窗口"><el-checkbox-group v-model="experimentForm.observation_windows"><el-checkbox v-for="value in ['2h','24h','72h']" :key="value" :value="value">{{value}}</el-checkbox></el-checkbox-group></el-form-item><el-form-item label="结论"><el-input v-model="experimentForm.conclusion" type="textarea" :rows="3" /></el-form-item></el-form>
    <template #footer><el-button @click="dialog=false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存</el-button></template>
  </el-dialog>
</template>

<style scoped>
.tab-actions{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:8px}.score-row{display:flex;flex-wrap:wrap;gap:7px}.score-row span{white-space:nowrap}.score-fields{display:grid;grid-template-columns:repeat(3,1fr);gap:0 12px}.muted{color:#7c8799}@media(max-width:680px){.tab-actions{align-items:flex-start;flex-direction:column}.score-fields{grid-template-columns:1fr 1fr}}
</style>
