<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { actionKey, formatBytes } from '../lib'
import type { Asset } from '../types'

const props = defineProps<{settings:any;assets:Asset[];saving:boolean;saveSettings:()=>Promise<boolean>}>()
const emit = defineEmits<{asset:[Asset]}>()
const profile = computed(()=>props.settings.local_ai)
type VoiceModel = 'qwen3'|'chatterbox'
const profileVoiceModel = computed<VoiceModel>(()=>profile.value.voice_model==='chatterbox'?'chatterbox':'qwen3')
const status = ref<any>()
const statusError = ref('')
const uploading = ref('')
const pending = ref<'voice'|'avatar'|'transcribe'|null>(null)
const jobId = ref('')
const step = ref('')
const progress = ref(0)
const taskError = ref('')
const testText = ref('你好，这是在这台电脑上生成的声音测试。接下来，我会用这个声音和你分享有用的技术资讯。')
const previewVoiceModel = ref<VoiceModel>(profileVoiceModel.value)
const previewEmotionStrength = ref(Number(profile.value.emotion_strength ?? 0.65))
const voicePreview = ref<Asset|null>(null)
const avatarPreview = ref<Asset|null>(null)
const elapsed = ref<number|null>(null)
const reference = computed(()=>props.assets.find(asset=>asset.id===profile.value.reference_audio_asset_id))
const portrait = computed(()=>props.assets.find(asset=>asset.id===profile.value.portrait_asset_id))
const busy = computed(()=>Boolean(pending.value)||Boolean(uploading.value)||props.saving)
const voiceComponent = (model:VoiceModel) => model==='chatterbox'?'chatterbox':'tts'
const voiceModelInstalled = (model:VoiceModel) => Boolean(status.value?.[voiceComponent(model)]?.installed)
const hasVoiceInput = (model:VoiceModel) => Boolean(reference.value) && (model==='chatterbox'||Boolean(profile.value.reference_text?.trim()))
const canPreviewVoice = computed(()=>!busy.value&&Boolean(testText.value.trim())&&hasVoiceInput(previewVoiceModel.value)&&voiceModelInstalled(previewVoiceModel.value)&&!status.value?.busy)
const canUseFutureVoice = computed(()=>!busy.value&&hasVoiceInput(profileVoiceModel.value)&&voiceModelInstalled(profileVoiceModel.value)&&!status.value?.busy)
// The native voice flag and the speech model source are one choice; keep the parent
// settings object consistent so a full settings save never mixes engines.
function syncSpeechSource(enabled:boolean) {
  const speech=props.settings.models?.speech
  if(!speech) return
  if(enabled) speech.source='builtin'
  else if(speech.source==='builtin') speech.source='third_party'
}
let timer:ReturnType<typeof setTimeout>|undefined
let disposed = false
const reasonText=(reason:string)=>({manifest_missing:'模型正在安装或尚未完成验证',manifest_invalid:'安装记录需要重新验证',component_not_installed:'该组件尚未完成安装验证',model_mismatch:'安装模型与当前配置不一致',runtime_missing:'本地运行环境缺失',model_missing:'模型文件尚未就绪'}[reason]||'本地环境需要检查')
watch([()=>profile.value.reference_audio_asset_id,()=>profile.value.reference_text,()=>testText.value,previewVoiceModel,previewEmotionStrength],()=>{voicePreview.value=null;avatarPreview.value=null;elapsed.value=null})
watch(()=>profile.value.portrait_asset_id,()=>{avatarPreview.value=null})

async function refreshStatus() {
  try { status.value=await api.localAIStatus();statusError.value='' }
  catch(error:any) { statusError.value=error.message }
}
async function upload(kind:'voice_reference'|'portrait', file:any) {
  if (busy.value) return
  uploading.value=kind
  try {
    const body=new FormData();body.append('file',file.raw);body.append('kind',kind)
    const asset:Asset=await api.uploadAsset(body)
    emit('asset',asset)
    if(kind==='voice_reference') {profile.value.reference_audio_asset_id=asset.id;profile.value.reference_text='';voicePreview.value=null}
    else profile.value.portrait_asset_id=asset.id
    avatarPreview.value=null
    ElMessage.success('素材已上传到本机，保存参考资料后可开始测试。')
  } catch(error:any) {ElMessage.error(error.message)}
  finally {uploading.value=''}
}
async function poll(id:string, kind:'voice'|'avatar'|'transcribe', sourceId:string|null) {
  if(disposed) return
  try {
    const job:any=await api.job(id)
    step.value=job.current_step || '等待本地任务';progress.value=job.progress || 0
    if(job.status==='SUCCEEDED') {
      const result=job.result || {}
      if(kind==='transcribe') {
        if(profile.value.reference_audio_asset_id===sourceId && result.source_asset_id===sourceId)
          profile.value.reference_text=result.transcript || ''
        ElMessage.success('已识别文字，请核对与录音一致后保存。')
      } else {
        const items=(await api.assets()).items
        const asset=items.find((item:any)=>item.id===result.asset_id)
        if(!asset) throw new Error('任务完成，但未找到生成素材，请在任务中心查看。')
        emit('asset',asset)
        if(kind==='voice')voicePreview.value=asset
        else avatarPreview.value=asset
        elapsed.value=typeof result.elapsed_seconds==='number'?result.elapsed_seconds:null
        ElMessage.success('本地生成完成，请试听或查看口型效果。')
      }
      pending.value=null;await refreshStatus();return
    }
    if(['FAILED','CANCELED','WAITING_USER'].includes(job.status)) {
      taskError.value=job.error_message || (job.status==='CANCELED'?'任务已取消':'本地任务需要处理，请查看任务中心。')
      pending.value=null;await refreshStatus();return
    }
    timer=setTimeout(()=>poll(id,kind,sourceId),2000)
  } catch(error:any) {taskError.value=error.message;pending.value=null}
}
async function start(kind:'voice'|'avatar'|'transcribe') {
  if(busy.value)return
  if(kind==='voice'&&!canPreviewVoice.value)return
  taskError.value='';elapsed.value=null
  if(!await props.saveSettings())return
  pending.value=kind;step.value='等待本地模型';progress.value=0
  try {
    const sourceId=profile.value.reference_audio_asset_id
    const result=kind==='transcribe'?await api.localTranscribe(sourceId,actionKey()):await api.localPreview({kind,text:testText.value,
      ...(kind==='voice'?{voice_model:previewVoiceModel.value,...(previewVoiceModel.value==='chatterbox'?{emotion_strength:previewEmotionStrength.value}:{})}:{}),
      ...(kind==='avatar'?{audio_asset_id:voicePreview.value?.id || sourceId}:{})},actionKey())
    jobId.value=result.job_id
    await poll(result.job_id,kind,sourceId)
  } catch(error:any) {taskError.value=error.message;pending.value=null}
}
async function cancel() {
  if(!jobId.value)return
  try {await api.cancelJob(jobId.value,actionKey());step.value='正在停止本地任务'}
  catch(error:any){ElMessage.error(error.message)}
}
onMounted(refreshStatus)
onBeforeUnmount(()=>{disposed=true;if(timer)clearTimeout(timer)})
</script>

<template>
<section class="local-ai-panel">
  <div class="local-ai-heading">
    <div><h3>本地本人声音与照片数字人</h3><p>上传参考录音和照片，先测试效果，再用于内容生产。生成过程禁止联网，不会转用云端接口。</p></div>
    <el-button @click="refreshStatus">刷新环境状态</el-button>
  </div>
  <el-alert v-if="statusError" :title="statusError" type="error" :closable="false"/>
  <div class="local-ai-status">
    <div v-for="item in [{key:'tts',name:'Qwen3 本人声音',detail:'Qwen3-TTS · Apple GPU'},{key:'chatterbox',name:'Chatterbox 本人声音',detail:'Chatterbox multilingual v3 · Apple GPU'},{key:'avatar',name:'照片数字人',detail:'MuseTalk · Apple GPU'},{key:'transcribe',name:'录音转文字',detail:'Whisper tiny · 本地 CPU'}]" :key="item.key">
      <b>{{item.name}}</b><el-tag :type="status?.[item.key]?.installed?'success':'info'">{{status?.[item.key]?.installed?'已安装':'等待安装验证'}}</el-tag>
      <small>{{item.detail}}</small><small v-if="status?.[item.key]?.reason">{{reasonText(status[item.key].reason)}}</small>
    </div>
  </div>
  <div class="local-ai-grid">
    <el-card shadow="never">
      <template #header><b>1. 本人参考录音</b></template>
      <p class="local-ai-help">使用 3～30 秒的单人清晰录音，建议 10～20 秒，避免背景音乐。支持 WAV、MP3、M4A 等格式。</p>
      <el-upload :auto-upload="false" :show-file-list="false" accept="audio/*" :disabled="busy" :on-change="(file:any)=>upload('voice_reference',file)">
        <el-button :loading="uploading==='voice_reference'" :disabled="busy">上传本人音频</el-button>
      </el-upload>
      <el-select v-model="profile.reference_audio_asset_id" clearable :disabled="busy" placeholder="或选择已上传的参考录音" @change="()=>{profile.reference_text='';voicePreview=null;avatarPreview=null}">
        <el-option v-for="asset in assets.filter(item=>item.kind==='voice_reference')" :key="asset.id" :label="asset.filename" :value="asset.id"/>
      </el-select>
      <div v-if="reference" class="local-ai-reference">
        <small>{{reference.filename}} · {{formatBytes(reference.size)}}</small>
        <audio controls :src="reference.url" preload="metadata"/>
      </div>
      <el-form label-position="top">
        <el-form-item label="录音对应文字（Qwen3 必填）">
          <el-input v-model="profile.reference_text" type="textarea" :rows="4" maxlength="2000" show-word-limit :disabled="busy" placeholder="可先自动识别，再对照录音修正。文字准确有助于保留声音特点。"/>
          <p class="local-ai-help">Chatterbox 试听和后续视频只需要参考录音，不使用这段文字。</p>
        </el-form-item>
      </el-form>
      <el-button :disabled="busy||!reference||!status?.transcribe?.installed||status?.busy" @click="start('transcribe')">自动识别录音文字</el-button>
    </el-card>
    <el-card shadow="never">
      <template #header><b>2. 本人照片</b></template>
      <p class="local-ai-help">使用单人正面照片，面部清晰、无遮挡，背景尽量简单。支持 JPG、PNG、WebP。</p>
      <el-upload :auto-upload="false" :show-file-list="false" accept="image/png,image/jpeg,image/webp" :disabled="busy" :on-change="(file:any)=>upload('portrait',file)">
        <el-button :loading="uploading==='portrait'" :disabled="busy">上传本人照片</el-button>
      </el-upload>
      <el-select v-model="profile.portrait_asset_id" clearable :disabled="busy" placeholder="或选择已上传照片" @change="()=>avatarPreview=null">
        <el-option v-for="asset in assets.filter(item=>item.kind==='portrait')" :key="asset.id" :label="asset.filename" :value="asset.id"/>
      </el-select>
      <div v-if="portrait" class="local-ai-portrait"><img :src="portrait.url" alt="本人参考照片"/><small>{{portrait.filename}}</small></div>
      <el-empty v-else description="照片会显示在这里" :image-size="70"/>
    </el-card>
  </div>
  <el-card shadow="never" class="local-ai-preview">
    <template #header><b>3. 试听与数字人预览</b></template>
    <el-alert type="info" :closable="false" title="试听可临时选择声音模型与表现力，不会改变后续视频使用的模型；请到第 4 部分保存后续设置。"/>
    <el-form label-position="top" class="local-ai-preview-options">
      <el-form-item label="试听声音模型" class="preview-model">
        <el-select v-model="previewVoiceModel" :disabled="busy">
          <el-option label="Qwen3（需录音对应文字）" value="qwen3"/>
          <el-option label="Chatterbox（仅需参考录音）" value="chatterbox"/>
        </el-select>
      </el-form-item>
      <el-form-item v-if="previewVoiceModel==='chatterbox'" label="Chatterbox 表现力强度" class="preview-emotion">
        <el-slider v-model="previewEmotionStrength" :min="0" :max="1" :step="0.05" show-input :disabled="busy"/>
      </el-form-item>
    </el-form>
    <el-input v-model="testText" type="textarea" :rows="2" maxlength="500" show-word-limit :disabled="busy" aria-label="本地试听文案"/>
    <div class="local-ai-actions">
      <el-button type="primary" :disabled="!canPreviewVoice" @click="start('voice')">生成本人声音试听</el-button>
      <el-button :disabled="busy||!portrait||!reference||!status?.avatar?.installed||status?.busy" @click="start('avatar')">生成照片数字人预览</el-button>
      <el-button v-if="pending" @click="cancel">取消当前测试</el-button>
    </div>
    <p class="local-ai-help">数字人预览优先使用本次声音试听；没有试听结果时使用参考录音。口型由音频驱动，照片中的头部与背景保持原有构图。</p>
    <div v-if="pending" class="local-ai-progress"><span>{{step}}</span><el-progress :percentage="progress"/><router-link :to="'/jobs'">在任务中心查看</router-link></div>
    <el-alert v-if="taskError" :title="taskError" type="error" :closable="false"/>
    <div class="local-ai-outputs">
      <div v-if="voicePreview"><b>本地声音试听</b><audio controls :src="voicePreview.url"/></div>
      <div v-if="avatarPreview"><b>照片数字人预览</b><video controls :src="avatarPreview.url" preload="metadata"/></div>
    </div>
    <small v-if="elapsed!==null">最近一次本地推理耗时：{{elapsed.toFixed(1)}} 秒</small>
  </el-card>
  <el-card shadow="never">
    <template #header><b>4. 用于后续视频</b></template>
    <el-form label-position="top">
      <el-form-item label="后续视频声音模型" class="future-model">
        <el-select v-model="profile.voice_model" :disabled="busy">
          <el-option label="Qwen3（需录音对应文字）" value="qwen3"/>
          <el-option label="Chatterbox（仅需参考录音）" value="chatterbox"/>
        </el-select>
      </el-form-item>
      <el-form-item v-if="profileVoiceModel==='chatterbox'" label="Chatterbox 表现力强度">
        <el-slider v-model="profile.emotion_strength" :min="0" :max="1" :step="0.05" show-input :disabled="busy"/>
      </el-form-item>
      <el-form-item label="后续新旁白使用本地本人声音" class="future-voice-switch"><el-switch v-model="profile.voice_enabled" :disabled="!canUseFutureVoice" @change="syncSpeechSource"/></el-form-item>
      <el-form-item label="在成片右下角叠加照片数字人"><el-switch v-model="profile.avatar_enabled" :disabled="busy||!portrait||!status?.avatar?.installed"/></el-form-item>
    </el-form>
    <p class="local-ai-help">这里保存的是后续新视频的模型选择。Qwen3 需要录音对应文字，Chatterbox 只需要参考录音；表现力强度只适用于 Chatterbox。已存在的配音，可在内容页明确替换后重新审核。</p>
    <el-button type="primary" :loading="saving" :disabled="Boolean(pending)||Boolean(uploading)" @click="saveSettings">保存参考资料与设置</el-button>
  </el-card>
</section>
</template>

<style scoped>
.local-ai-panel{display:grid;gap:20px}.local-ai-heading{display:flex;justify-content:space-between;gap:24px;align-items:flex-start}.local-ai-heading h3{margin:0 0 8px}.local-ai-heading p,.local-ai-help{color:#68758a;line-height:1.7;font-size:13px}.local-ai-status{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.local-ai-status>div{padding:18px;border:1px solid #e5e9f1;border-radius:12px;background:#fafcff;display:flex;gap:8px;align-items:center;flex-wrap:wrap}.local-ai-status small{width:100%;color:#718097}.local-ai-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.local-ai-panel .el-select{display:block;width:100%;margin:14px 0}.local-ai-preview-options{margin-top:14px}.local-ai-reference{display:grid;gap:10px;margin:15px 0}.local-ai-reference small,.local-ai-portrait small{color:#718097}.local-ai-portrait{display:grid;justify-items:center;gap:10px;padding:18px}.local-ai-portrait img{max-width:100%;height:240px;object-fit:contain;border-radius:12px}.local-ai-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}.local-ai-outputs{display:flex;gap:24px;flex-wrap:wrap;margin-top:20px}.local-ai-outputs>div{display:grid;gap:12px;flex:1;min-width:260px}.local-ai-outputs video{max-width:100%;max-height:420px;border-radius:12px;background:#111}.local-ai-panel audio{max-width:100%;width:100%}.local-ai-progress{margin:18px 0;display:grid;gap:10px}@media(max-width:1200px){.local-ai-status{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:950px){.local-ai-grid,.local-ai-status{grid-template-columns:1fr}.local-ai-heading{flex-direction:column}}
</style>
