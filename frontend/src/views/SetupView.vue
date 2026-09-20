<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRoute } from 'vue-router';
import { ElMessage } from 'element-plus';
import { api } from '../api';
import { formatBytes, formatDate } from '../lib';
import type { Asset, Template } from '../types';
import PageIntro from '../components/PageIntro.vue';
import StatePanel from '../components/StatePanel.vue';
import LocalAISettings from '../components/LocalAISettings.vue';
const route = useRoute();
const tab = ref(route.query.tab==='local-ai'?'local-ai':'templates');
const templates = ref<Template[]>([]);
const assets = ref<Asset[]>([]);
const settings = ref<any>();
const diagnostics = ref<any>();
const schedules = ref<any[]>([]);
const loading = ref(false);
const error = ref('');
const settingsSaving = ref(false);
const templateSaving = ref(false);
const scheduleSaving = ref(false);
const uploadKind = ref<'bgm' | 'logo' | null>(null);
const scheduleChanging = ref<string | null>(null);
const templateDialog = ref(false);
const versionsDialog = ref(false);
const scheduleDialog = ref(false);
const editingTemplate = ref<Template | null>(null);
const editingSchedule = ref<any | null>(null);
const versions = ref<any[]>([]);
const templateForm = ref<any>({ name: '', description: '', prompt: '', style: 'brief' });
const scheduleForm = ref<any>({ name: '', kind: 'pipeline', time: '09:00', timezone: '', enabled: false, daily_limit: 1 });
const modelLabel = (key: unknown) => ({ text: '文本模型', image: '图像模型', speech: '语音模型' }[String(key)] || '模型');
const protocols = (key: unknown) => String(key) === 'text' ? [{ value: 'openai', label: 'OpenAI 兼容' }] : String(key) === 'image' ? [{ value: 'openai', label: 'OpenAI 兼容' }, { value: 'ark', label: '火山方舟' }] : [{ value: 'openai', label: 'OpenAI 兼容' }, { value: 'dashscope', label: '百炼原生' }];
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));
async function load() {
    loading.value = true;
    error.value = '';
    try {
        const [templateResponse, settingsResponse, diagnosticsResponse, scheduleResponse, assetResponse] = await Promise.all([api.templates(), api.settings(), api.diagnostics(), api.schedules(), api.assets()]);
        templates.value = templateResponse.items;
        settings.value = settingsResponse;
        settings.value.publishing ??= { auto_publish_after_approval: false };
        settings.value.local_ai = { voice_enabled: false, avatar_enabled: false, reference_audio_asset_id: null, reference_text: '', portrait_asset_id: null, voice_model: 'qwen3', emotion_strength: 0.65, ...(settings.value.local_ai || {}) };
        for (const model of Object.values(settings.value.models) as any[]) {
            model.protocol ??= 'openai';
        }
        diagnostics.value = diagnosticsResponse;
        schedules.value = scheduleResponse.items;
        assets.value = assetResponse.items;
        if (!scheduleForm.value.timezone)
            scheduleForm.value.timezone = settingsResponse.timezone;
    }
    catch (exception: any) {
        error.value = exception.message;
    }
    finally {
        loading.value = false;
    }
}
async function saveSettings() {
    if (!settings.value || settingsSaving.value)
        return false;
    settingsSaving.value = true;
    try {
        const models: any = {};
        for (const [name, config] of Object.entries(settings.value.models)) {
            const { key_configured, ...editable } = config as any;
            models[name] = editable;
        }
        const saved = await api.patchSettings({ models, video: settings.value.video, local_ai: settings.value.local_ai, publishing: settings.value.publishing, limits: settings.value.limits, timezone: settings.value.timezone });
        settings.value = saved;
        ElMessage.success('设置已保存；保存不会触发服务商连接或计费测试。');
        return true;
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
        return false;
    }
    finally {
        settingsSaving.value = false;
    }
}
function openTemplate(template?: Template) {
    editingTemplate.value = template || null;
    templateForm.value = template ? clone(template) : { name: '', description: '', prompt: '', style: 'brief' };
    templateDialog.value = true;
}
async function saveTemplate() {
    if (templateSaving.value)
        return;
    templateSaving.value = true;
    try {
        if (editingTemplate.value)
            await api.patchTemplate(editingTemplate.value.id, templateForm.value);
        else
            await api.createTemplate(templateForm.value);
        templateDialog.value = false;
        await load();
        ElMessage.success(editingTemplate.value ? '模板已更新' : '模板已创建');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        templateSaving.value = false;
    }
}
async function openVersions(template: Template) {
    try {
        versions.value = (await api.templateVersions(template.id)).items;
        versionsDialog.value = true;
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
}
function openSchedule(schedule?: any) {
    editingSchedule.value = schedule || null;
    scheduleForm.value = schedule ? clone(schedule) : { name: '', kind: 'pipeline', time: '09:00', timezone: settings.value?.timezone || '', enabled: false, daily_limit: 1 };
    scheduleDialog.value = true;
}
async function saveSchedule() {
    if (scheduleSaving.value)
        return;
    scheduleSaving.value = true;
    try {
        if (editingSchedule.value)
            await api.patchSchedule(editingSchedule.value.id, scheduleForm.value);
        else
            await api.createSchedule(scheduleForm.value);
        scheduleDialog.value = false;
        await load();
        ElMessage.success(editingSchedule.value ? '计划已更新' : '计划已创建');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        scheduleSaving.value = false;
    }
}
async function toggleSchedule(schedule: any, enabled: boolean) {
    if (scheduleChanging.value)
        return;
    const previous = !enabled;
    scheduleChanging.value = schedule.id;
    try {
        const updated = await api.patchSchedule(schedule.id, { enabled });
        const index = schedules.value.findIndex(item => item.id === schedule.id);
        if (index >= 0)
            schedules.value.splice(index, 1, updated);
    }
    catch (exception: any) {
        schedule.enabled = previous;
        ElMessage.error(exception.message);
    }
    finally {
        scheduleChanging.value = null;
    }
}
async function uploadVideoAsset(kind: 'bgm' | 'logo', file: any) {
    if (uploadKind.value)
        return;
    uploadKind.value = kind;
    try {
        const form = new FormData();
        form.append('file', file.raw);
        form.append('kind', kind);
        const uploaded = await api.uploadAsset(form);
        assets.value.push(uploaded);
        if (settings.value)
            settings.value.video[kind === 'bgm' ? 'bgm_asset_id' : 'logo_asset_id'] = uploaded.id;
        ElMessage.success('素材已上传，保存设置后会用于后续视频。');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        uploadKind.value = null;
    }
}
async function inspect() {
    try {
        diagnostics.value = await api.diagnostics();
        ElMessage.success('已刷新诊断');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
}
onMounted(load);
</script>

<template>
<PageIntro title="模板与设置" description="配置内容模板、本地模型参数、日限额和计划。密钥只通过环境变量提供，不会显示或输入到此页面。">
  <el-button @click="load">刷新</el-button>
</PageIntro>
<el-tabs v-model="tab">
  <el-tab-pane label="内容模板" name="templates"/>
  <el-tab-pane label="模型与视频" name="settings"/>
  <el-tab-pane label="本人声音与数字人" name="local-ai"/>
  <el-tab-pane label="定时计划" name="schedules"/>
  <el-tab-pane label="运行诊断" name="diagnostics"/>
</el-tabs>
<StatePanel :loading :error @retry="load">
  <LocalAISettings v-if="tab==='local-ai' && settings" :settings="settings" :assets="assets" :saving="settingsSaving" :save-settings="saveSettings" @asset="(asset:Asset)=>{if(!assets.some(item=>item.id===asset.id))assets.push(asset)}"/>
  <section v-if="tab==='templates'">
    <div class="section-title">
      <h3>内容模板</h3>
      <el-button type="primary" @click="openTemplate()">新建模板</el-button>
    </div>
    <el-empty v-if="!templates.length" description="暂无模板，可创建内容生产提示模板。"/>
    <el-table v-else :data="templates" class="data-table">
      <el-table-column prop="name" label="名称" min-width="170"/>
      <el-table-column prop="style" label="风格" width="110"/>
      <el-table-column prop="description" label="说明" min-width="250"/>
      <el-table-column prop="version" label="当前版本" width="100"/>
      <el-table-column label="更新时间" width="170">
        <template #default="{row}">{{formatDate(row.updated_at)}}</template>
      </el-table-column>
      <el-table-column label="操作" width="145">
        <template #default="{row}">
          <el-button link type="primary" @click="openTemplate(row)">编辑</el-button>
          <el-button link @click="openVersions(row)">版本</el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
  <section v-if="tab==='settings' && settings">
    <el-alert
      type="info"
      :closable="false"
      title="模型密钥由 PUBLISHER_TEXT_API_KEY、PUBLISHER_IMAGE_API_KEY、PUBLISHER_SPEECH_API_KEY 环境变量提供。页面不会接收、展示或保存密钥。"
    />
    <h3>模型配置</h3>
    <div class="model-grid">
      <el-card v-for="(model,key) in settings.models" :key="String(key)" shadow="never">
        <template #header>{{modelLabel(key)}}</template>
        <el-form label-position="top">
          <el-form-item label="接入协议">
            <el-select v-model="model.protocol">
              <el-option v-for="protocol in protocols(key)" :key="protocol.value" :label="protocol.label" :value="protocol.value"/>
            </el-select>
          </el-form-item>
          <el-form-item label="服务地址">
            <el-input v-model="model.base_url" placeholder="https://…"/>
          </el-form-item>
          <el-form-item label="模型">
            <el-input v-model="model.model"/>
          </el-form-item>
          <el-form-item label="超时（秒）">
            <el-input-number v-model="model.timeout_seconds" :min="10" :max="900"/>
          </el-form-item>
          <el-form-item v-if="String(key)==='speech'" label="音色">
            <el-input v-model="model.voice" maxlength="100" placeholder="系统音色或本人复刻音色 ID"/>
            <p class="form-help">本人声音复刻完成后，在这里填写该模型对应的音色 ID。</p>
          </el-form-item>
          <el-form-item v-if="String(key)==='speech' && model.protocol==='dashscope' && model.model?.startsWith('qwen-audio-3.0-tts-')" label="朗读指导">
            <el-input v-model="model.instruction" type="textarea" :rows="3" maxlength="200" placeholder="像给朋友讲科技新闻，自然、有起伏，关键词轻微重读，句间自然停顿。"/>
          </el-form-item>
          <el-form-item v-if="String(key)==='speech' && model.protocol==='dashscope'" label="语言类型">
            <el-select v-model="model.language_type" placeholder="由后端配置">
              <el-option label="Chinese" value="Chinese"/>
            </el-select>
          </el-form-item>
          <el-form-item v-if="String(key)==='image'" label="图像尺寸">
            <el-select v-model="model.size" allow-create filterable>
              <el-option label="2K（Seedream）" value="2K"/>
              <el-option label="4K（Seedream）" value="4K"/>
              <el-option label="1024 × 1536" value="1024x1536"/>
              <el-option label="1024 × 1024" value="1024x1024"/>
              <el-option label="1536 × 1024" value="1536x1024"/>
            </el-select>
          </el-form-item>
          <el-form-item v-if="String(key)==='image' && model.protocol==='ark'" label="图片角落“AI生成”字样">
            <el-switch v-model="model.watermark"/>
            <p class="form-help">用于之后生成的图片；关闭不会修改已有图片。平台的 AI 内容声明独立保留。</p>
          </el-form-item>
          <el-form-item v-if="String(key)==='text'" label="最大输出 Token（可留空）">
            <el-input-number v-model="model.max_completion_tokens" :min="256" :max="32768" clearable/>
          </el-form-item>
          <el-form-item v-if="String(key)==='text'" label="思考模式（服务商支持时）">
            <el-select v-model="model.enable_thinking">
              <el-option label="服务默认，不传此参数" :value="null"/>
              <el-option label="关闭" :value="false"/>
              <el-option label="开启" :value="true"/>
            </el-select>
          </el-form-item>
          <el-form-item v-if="String(key)==='text'" label="结构化 JSON 输出">
            <el-switch v-model="model.json_mode"/>
          </el-form-item>
          <el-form-item label="启用 API 调用">
            <el-switch v-model="model.enabled"/>
            <p class="form-help">启用后，生成内容或日报可能产生服务商费用。</p>
          </el-form-item>
          <el-tag :type="model.key_configured?'success':'info'">{{model.key_configured?'环境变量已配置':'未检测到环境变量'}}</el-tag>
        </el-form>
      </el-card>
    </div>
    <h3>视频与额度</h3>
    <el-form label-position="top" class="settings-form">
      <el-form-item label="审核通过后自动发布">
        <el-switch v-model="settings.publishing.auto_publish_after_approval"/>
        <p class="form-help">开启后，新通过审核的内容会自动排队上传并点击最终发布，使用已绑定的平台账号。发布结果不明确时暂停核验，避免重复提交。</p>
      </el-form-item>
      <div class="form-grid">
        <el-form-item label="宽">
          <el-input-number v-model="settings.video.width" :min="320" :max="2160" :step="2"/>
        </el-form-item>
        <el-form-item label="高">
          <el-input-number v-model="settings.video.height" :min="320" :max="3840" :step="2"/>
        </el-form-item>
        <el-form-item label="帧率">
          <el-input-number v-model="settings.video.fps" :min="24" :max="60"/>
        </el-form-item>
        <el-form-item label="目标时长（秒）">
          <el-input-number v-model="settings.video.target_seconds" :min="15" :max="180"/>
        </el-form-item>
        <el-form-item label="旁白节奏">
          <el-select v-model="settings.video.timing_mode">
            <el-option label="保持自然语速（推荐）" value="natural"/>
            <el-option label="变速匹配目标时长" value="fit_target"/>
          </el-select>
          <p class="form-help">自然模式按实际配音时长成片；目标时长用于规划文案，避免强行拉长声音。</p>
        </el-form-item>
        <el-form-item label="每日模型/数据调用上限">
          <el-input-number v-model="settings.limits.daily_calls" :min="1" :max="500"/>
        </el-form-item>
        <el-form-item label="每日视频上限">
          <el-input-number v-model="settings.limits.daily_videos" :min="1" :max="20"/>
        </el-form-item>
      </div>
      <el-form-item label="字体路径">
        <el-input v-model="settings.video.font_path" placeholder="本机 .ttf、.ttc 或 .otf 的绝对路径"/>
        <p class="form-help">仅接受本机存在的 TTF、TTC 或 OTF 字体文件。</p>
      </el-form-item>
      <div class="video-assets">
        <el-form-item label="背景音乐">
          <el-select v-model="settings.video.bgm_asset_id" clearable placeholder="选择 BGM 或音频素材">
            <el-option
              v-for="asset in assets.filter(item=>item.kind==='bgm'||item.kind==='audio')"
              :key="asset.id"
              :value="asset.id"
              :label="asset.filename"
            />
          </el-select>
          <el-upload
            :show-file-list="false"
            :auto-upload="false"
            :on-change="(file:any)=>uploadVideoAsset('bgm',file)"
            :disabled="!!uploadKind"
          >
            <el-button :loading="uploadKind==='bgm'">上传 BGM</el-button>
          </el-upload>
        </el-form-item>
        <el-form-item label="角标 Logo">
          <el-select v-model="settings.video.logo_asset_id" clearable placeholder="选择 Logo 或图片素材">
            <el-option
              v-for="asset in assets.filter(item=>item.kind==='logo'||item.kind==='image')"
              :key="asset.id"
              :value="asset.id"
              :label="asset.filename"
            />
          </el-select>
          <el-upload
            :show-file-list="false"
            :auto-upload="false"
            :on-change="(file:any)=>uploadVideoAsset('logo',file)"
            :disabled="!!uploadKind"
          >
            <el-button :loading="uploadKind==='logo'">上传 Logo</el-button>
          </el-upload>
        </el-form-item>
      </div>
      <el-form-item label="时区">
        <el-input v-model="settings.timezone"/>
      </el-form-item>
      <el-button type="primary" :loading="settingsSaving" @click="saveSettings">保存设置</el-button>
    </el-form>
  </section>
  <section v-if="tab==='schedules'">
    <div class="section-title">
      <h3>定时计划</h3>
      <el-button type="primary" @click="openSchedule()">新建计划</el-button>
    </div>
    <el-alert type="warning" :closable="false" title="启用计划后会按照每日调用上限执行；流水线计划需要已经配置并启用所需模型。"/>
    <el-empty v-if="!schedules.length" description="暂无定时计划"/>
    <el-table v-else :data="schedules" class="data-table">
      <el-table-column prop="name" label="名称"/>
      <el-table-column prop="kind" label="类型"/>
      <el-table-column prop="time" label="时间"/>
      <el-table-column prop="timezone" label="时区"/>
      <el-table-column label="启用" width="110">
        <template #default="{row}">
          <el-switch
            v-model="row.enabled"
            :loading="scheduleChanging===row.id"
            @change="(enabled:boolean)=>toggleSchedule(row,enabled)"
          />
        </template>
      </el-table-column>
      <el-table-column label="下次执行" width="170">
        <template #default="{row}">{{formatDate(row.next_run_at)}}</template>
      </el-table-column>
      <el-table-column label="操作" width="80">
        <template #default="{row}">
          <el-button link @click="openSchedule(row)">编辑</el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
  <section v-if="tab==='diagnostics' && diagnostics">
    <div class="section-title">
      <h3>运行诊断</h3>
      <el-button @click="inspect">重新检查</el-button>
    </div>
    <div class="diagnostics">
      <article>
        <span>Python</span>
        <b>{{diagnostics.python}}</b>
      </article>
      <article>
        <span>FFmpeg</span>
        <b>{{diagnostics.ffmpeg?.available?'可用':'不可用'}}</b>
        <small>{{diagnostics.ffmpeg?.version||''}}</small>
      </article>
      <article>
        <span>ADB</span>
        <b>{{diagnostics.adb?.available?'可用':'不可用'}}</b>
      </article>
      <article>
        <span>可用存储</span>
        <b>{{formatBytes(diagnostics.storage?.free_bytes)}}</b>
      </article>
    </div>
    <el-alert v-for="warning in diagnostics.warnings" :key="warning" type="warning" :title="warning" :closable="false"/>
    <pre class="diagnostic-json">{{JSON.stringify(diagnostics.workers,null,2)}}</pre>
  </section>
</StatePanel>
<el-dialog v-model="templateDialog" :title="editingTemplate?'编辑内容模板':'新建内容模板'" width="520px">
  <el-form label-position="top">
    <el-form-item label="名称">
      <el-input v-model="templateForm.name"/>
    </el-form-item>
    <el-form-item label="说明">
      <el-input v-model="templateForm.description"/>
    </el-form-item>
    <el-form-item label="风格">
      <el-radio-group v-model="templateForm.style">
        <el-radio value="brief">简洁</el-radio>
        <el-radio value="explain">讲解</el-radio>
      </el-radio-group>
    </el-form-item>
    <el-form-item label="提示词">
      <el-input v-model="templateForm.prompt" type="textarea" :rows="6"/>
    </el-form-item>
  </el-form>
  <template #footer>
    <el-button :disabled="templateSaving" @click="templateDialog=false">取消</el-button>
    <el-button
      type="primary"
      :loading="templateSaving"
      :disabled="!templateForm.name||!templateForm.prompt"
      @click="saveTemplate"
    >
      保存
    </el-button>
  </template>
</el-dialog>
<el-dialog v-model="versionsDialog" title="模板版本记录" width="620px">
  <el-empty v-if="!versions.length" description="暂无历史版本"/>
  <el-table v-else :data="versions" size="small">
    <el-table-column prop="version" label="版本" width="80"/>
    <el-table-column prop="name" label="名称"/>
    <el-table-column prop="style" label="风格" width="90"/>
    <el-table-column label="更新时间" width="170">
      <template #default="{row}">{{formatDate(row.updated_at || row.created_at)}}</template>
    </el-table-column>
  </el-table>
</el-dialog>
<el-dialog v-model="scheduleDialog" :title="editingSchedule?'编辑定时计划':'新建定时计划'" width="480px">
  <el-alert type="warning" :closable="false" title="开启后会按每日调用上限执行，可能产生服务商费用。"/>
  <el-form label-position="top">
    <el-form-item label="名称">
      <el-input v-model="scheduleForm.name"/>
    </el-form-item>
    <el-form-item label="任务">
      <el-radio-group v-model="scheduleForm.kind">
        <el-radio value="pipeline">内容流水线</el-radio>
        <el-radio value="metrics">数据采集</el-radio>
      </el-radio-group>
    </el-form-item>
    <el-form-item label="每天执行时间">
      <el-time-picker v-model="scheduleForm.time" value-format="HH:mm" format="HH:mm"/>
    </el-form-item>
    <el-form-item label="时区">
      <el-input v-model="scheduleForm.timezone"/>
    </el-form-item>
    <el-form-item label="每日上限">
      <el-input-number v-model="scheduleForm.daily_limit" :min="1" :max="20"/>
    </el-form-item>
    <el-checkbox v-model="scheduleForm.enabled">保存后立即启用</el-checkbox>
  </el-form>
  <template #footer>
    <el-button :disabled="scheduleSaving" @click="scheduleDialog=false">取消</el-button>
    <el-button
      type="primary"
      :loading="scheduleSaving"
      :disabled="!scheduleForm.name||!scheduleForm.time||!scheduleForm.timezone"
      @click="saveSchedule"
    >
      保存
    </el-button>
  </template>
</el-dialog>
</template>
