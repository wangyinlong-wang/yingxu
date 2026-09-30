<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRoute } from 'vue-router';
import { ElMessage } from 'element-plus';
import { api } from '../api';
import { formatBytes, formatDate, intervalLabel, scheduleKindLabel, tikhubKeyText } from '../lib';
import type { KeyStatus } from '../types';
import type { Asset, Template } from '../types';
import PageIntro from '../components/PageIntro.vue';
import StatePanel from '../components/StatePanel.vue';
import LocalAISettings from '../components/LocalAISettings.vue';
const route = useRoute();
const knownTabs = ['templates', 'settings', 'local-ai', 'schedules', 'diagnostics'];
const tab = ref(knownTabs.includes(String(route.query.tab)) ? String(route.query.tab) : 'templates');
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
const intervalOptions = [30, 60, 120, 180, 360, 720, 1440];
const scheduleForm = ref<any>({ name: '', kind: 'pipeline', time: '09:00', timezone: '', enabled: false, daily_limit: 1, interval_minutes: null, include_paid: false });
const modelLabel = (key: unknown) => ({ text: '文本模型', image: '图像模型', speech: '语音模型', video: '视频模型 · MiniMax H3' }[String(key)] || '模型');
const protocols = (key: unknown) => String(key) === 'video' ? [{ value: 'minimax_h3', label: 'MiniMax H3 V2（Metaso）' }] : String(key) === 'text' ? [{ value: 'openai', label: 'OpenAI 兼容' }] : String(key) === 'image' ? [{ value: 'openai', label: 'OpenAI 兼容' }, { value: 'ark', label: '火山方舟' }] : [{ value: 'openai', label: 'OpenAI 兼容' }, { value: 'dashscope', label: '百炼原生' }];
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));
const profileOf = (model: any) => model?.source === 'local' ? model.local : model;
const keyDrafts = ref<Record<string, string>>({});
const tikhubStatus = ref<KeyStatus | null>(null), tikhubDraft = ref(''), tikhubSaving = ref(false);
const keySaving = ref('');
const keySlot = (kind: string, model: any) => `${kind}:${model?.source === 'local' ? 'local' : 'third_party'}`;
const CREDENTIAL_META = new Set(['key_configured', 'key_source', 'secure_store_available', 'key_base_url']);
function stripCredentialMeta(value: any): any {
    if (Array.isArray(value))
        return value.map(stripCredentialMeta);
    if (value && typeof value === 'object') {
        const cleaned: any = {};
        for (const [name, item] of Object.entries(value))
            if (!CREDENTIAL_META.has(name))
                cleaned[name] = stripCredentialMeta(item);
        return cleaned;
    }
    return value;
}
function localProfileDefaults() {
    return { protocol: 'openai', base_url: '', model: '', timeout_seconds: 180, voice: 'alloy', size: '1024x1536', json_mode: false, enabled: false, watermark: false, instruction: '', language_type: 'Chinese', max_completion_tokens: null, enable_thinking: null };
}
function normalizeSettings(raw: any) {
    const value = raw ?? {};
    value.publishing ??= { auto_publish_after_approval: false };
    value.local_ai = { voice_enabled: false, avatar_enabled: false, reference_audio_asset_id: null, reference_text: '', portrait_asset_id: null, voice_model: 'qwen3', emotion_strength: 0.65, ...(value.local_ai || {}) };
    for (const [kind, model] of Object.entries(value.models || {}) as [string, any][]) {
        model.protocol ??= 'openai';
        // A legacy response without per-model sources still honors the old native voice flag.
        model.source ??= kind === 'speech' && value.local_ai.voice_enabled ? 'builtin' : 'third_party';
        model.local = { ...localProfileDefaults(), ...(model.local || {}), protocol: 'openai' };
    }
    return value;
}
function onSourceChange(kind: string, model: any) {
    if (model.source === 'local')
        model.local.protocol = 'openai';
    if (kind === 'speech' && settings.value?.local_ai)
        settings.value.local_ai.voice_enabled = model.source === 'builtin';
}
function keyTagType(profile: any) {
    if (profile?.key_configured)
        return 'success';
    return profile?.secure_store_available === false ? 'warning' : 'info';
}
function keyStatusText(kind: string) {
    const model = settings.value?.models?.[kind];
    const profile = model ? profileOf(model) : {};
    if (profile.key_source === 'keychain')
        return '钥匙串已保存密钥';
    if (profile.key_source === 'environment' || (profile.key_source === undefined && profile.key_configured))
        return '使用环境变量密钥';
    if (profile.secure_store_available === false)
        return '钥匙串不可用';
    return model?.source === 'local' ? '未保存密钥（本地服务可选）' : '未配置密钥';
}
function liveModel(model: any) {
    return !!settings.value?.models && Object.values(settings.value.models).includes(model);
}
function bindKeyStatus(target: { model: any; profile: any; base_url: string }, status: any) {
    // Bind returned metadata to the exact submitted target: the settings tree may have been
    // replaced meanwhile, and the Base URL may have moved on (the form marks that as stale).
    if (!liveModel(target.model))
        return;
    Object.assign(target.profile, status, { key_base_url: target.base_url });
}
async function saveModelKey(kind: string) {
    const model = settings.value?.models?.[kind];
    if (!model || keySaving.value)
        return;
    const profile = profileOf(model);
    const slot = keySlot(kind, model);
    const apiKey = keyDrafts.value[slot] || '';
    if (!profile.base_url) {
        ElMessage.warning('请先填写 Base URL，密钥会与其绑定。');
        return;
    }
    if (!apiKey) {
        ElMessage.warning('请先输入 API Key。');
        return;
    }
    // Capture the exact credential target at submit time; the user may keep editing meanwhile.
    const target = { model, profile, source: model.source as 'third_party' | 'local', base_url: profile.base_url as string, draft: apiKey };
    keySaving.value = slot;
    try {
        const status = await api.saveModelKey(kind, { source: target.source, base_url: target.base_url, action: 'set', api_key: target.draft });
        bindKeyStatus(target, status);
        // Clear only the draft that was actually submitted; keep anything typed meanwhile.
        if (keyDrafts.value[slot] === target.draft)
            keyDrafts.value[slot] = '';
        ElMessage.success('密钥已保存到本机钥匙串；未发起任何 API 调用。');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        keySaving.value = '';
    }
}
async function clearModelKey(kind: string) {
    const model = settings.value?.models?.[kind];
    if (!model || keySaving.value)
        return;
    const profile = profileOf(model);
    if (!profile.base_url) {
        ElMessage.warning('请先填写 Base URL。');
        return;
    }
    const slot = keySlot(kind, model);
    const target = { model, profile, source: model.source as 'third_party' | 'local', base_url: profile.base_url as string };
    keySaving.value = slot;
    try {
        const status = await api.saveModelKey(kind, { source: target.source, base_url: target.base_url, action: 'clear' });
        bindKeyStatus(target, status);
        ElMessage.success(status.key_source === 'environment' ? '已清除钥匙串密钥；环境变量密钥仍在生效。' : '已清除该地址保存的密钥。');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        keySaving.value = '';
    }
}
async function loadTikhubStatus() {
    try {
        tikhubStatus.value = await api.tikhubKeyStatus();
    } catch {
        tikhubStatus.value = null;
    }
}
async function saveTikhubKey(action: 'set' | 'clear') {
    if (tikhubSaving.value || (action === 'set' && !tikhubDraft.value.trim()))
        return;
    tikhubSaving.value = true;
    try {
        tikhubStatus.value = await api.saveTikhubKey(action === 'set' ? { action, api_key: tikhubDraft.value.trim() } : { action });
        tikhubDraft.value = '';
        ElMessage.success(action === 'set' ? 'TikHub 密钥已保存到钥匙串，下一次采集即生效。' : tikhubStatus.value.key_source === 'environment' ? '已清除钥匙串密钥；.env 中的环境变量密钥仍在生效。' : '已清除 TikHub 密钥。');
    } catch (exception: any) {
        ElMessage.error(exception.message);
    } finally {
        tikhubSaving.value = false;
    }
}
async function load() {
    loading.value = true;
    error.value = '';
    try {
        const [templateResponse, settingsResponse, diagnosticsResponse, scheduleResponse, assetResponse] = await Promise.all([api.templates(), api.settings(), api.diagnostics(), api.schedules(), api.assets()]);
        void loadTikhubStatus();
        templates.value = templateResponse.items;
        settings.value = normalizeSettings(settingsResponse);
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
    if (keySaving.value) {
        // A key save in flight must not let an ordinary settings save replace the model
        // objects its returned metadata is bound to.
        ElMessage.warning('密钥操作进行中，请稍候再保存设置。');
        return false;
    }
    settingsSaving.value = true;
    try {
        const payload = stripCredentialMeta({ models: settings.value.models, video: settings.value.video, local_ai: settings.value.local_ai, publishing: settings.value.publishing, limits: settings.value.limits, timezone: settings.value.timezone });
        const saved = await api.patchSettings(payload);
        settings.value = normalizeSettings(saved);
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
    scheduleForm.value = schedule ? clone(schedule) : { name: '', kind: 'pipeline', time: '09:00', timezone: settings.value?.timezone || '', enabled: false, daily_limit: 1, interval_minutes: null, include_paid: false };
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
<PageIntro title="模板与设置" description="配置内容模板、模型来源与参数、日限额和计划。API Key 经各卡片「保存密钥」写入本机钥匙串，不会进入设置数据或页面存储。">
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
      title="API Key 通过每张卡片的「保存密钥」写入 macOS 钥匙串并与当前 Base URL 绑定；第三方模型也可继续使用 PUBLISHER_TEXT_API_KEY、PUBLISHER_IMAGE_API_KEY、PUBLISHER_SPEECH_API_KEY、PUBLISHER_VIDEO_API_KEY 环境变量。密钥不会进入设置数据、数据库、日志或备份。请先保存密钥再保存设置；两步都不会发起任何 API 调用。"
    />
    <h3>模型配置</h3>
    <div class="model-grid">
      <el-card v-for="(model,key) in settings.models" :key="String(key)" shadow="never">
        <template #header>{{modelLabel(key)}}</template>
        <el-form label-position="top">
          <el-form-item label="模型来源">
            <el-select v-model="model.source" @change="()=>onSourceChange(String(key),model)">
              <el-option label="第三方模型" value="third_party"/>
              <el-option v-if="String(key)!=='video'" label="本地 URL 服务" value="local"/>
              <el-option v-if="String(key)==='speech'" label="本地声音引擎" value="builtin"/>
            </el-select>
          </el-form-item>
          <template v-if="model.source==='builtin'">
            <el-alert type="info" :closable="false" title="已选择本机运行的本人声音引擎：不产生 API 调用，但需要已完成验证的本人参考录音。"/>
            <el-button class="builtin-link" @click="tab='local-ai'">打开「本人声音与数字人」</el-button>
          </template>
          <template v-else>
            <el-form-item v-if="model.source==='third_party'" label="接入协议">
              <el-select v-model="model.protocol">
                <el-option v-for="protocol in protocols(key)" :key="protocol.value" :label="protocol.label" :value="protocol.value"/>
              </el-select>
            </el-form-item>
            <el-form-item v-else label="接入协议">
              <el-input model-value="OpenAI 兼容" disabled/>
            </el-form-item>
            <p v-if="String(key)==='video'" class="form-help">使用 MiniMax H3 V2：Base URL 填 https://metaso.cn/api/minimax，模型名填 MiniMax-H3。启用后可在内容分镜中单独生成视频，不会随一键生成自动调用。</p>
            <el-form-item label="Base URL">
              <el-input v-model="profileOf(model).base_url" :placeholder="model.source==='local'?'http://127.0.0.1:8000/v1':'https://…'"/>
              <p v-if="model.source==='local'" class="form-help">仅限本机回环地址（localhost、127.0.0.1 或 ::1），可使用 HTTP 或 HTTPS。</p>
            </el-form-item>
            <el-form-item label="模型名称">
              <el-input v-model="profileOf(model).model"/>
            </el-form-item>
            <el-form-item label="API Key">
              <div class="key-row">
                <el-input
                  v-model="keyDrafts[keySlot(String(key),model)]"
                  type="password"
                  show-password
                  :placeholder="profileOf(model).key_configured?'已保存密钥；输入新密钥可替换':'输入密钥后点击保存密钥'"
                />
                <el-button :loading="keySaving===keySlot(String(key),model)" @click="saveModelKey(String(key))">保存密钥</el-button>
                <el-button :disabled="profileOf(model).key_source!=='keychain'" @click="clearModelKey(String(key))">清除已保存密钥</el-button>
              </div>
              <el-tag :type="keyTagType(profileOf(model))">{{keyStatusText(String(key))}}</el-tag>
              <p v-if="profileOf(model).key_base_url!==undefined && profileOf(model).key_base_url!==profileOf(model).base_url" class="form-help key-stale">Base URL 已修改；上方密钥状态仍对应原地址，请按新地址保存密钥，或保存设置后刷新确认。</p>
              <p class="form-help">{{model.source==='local'?'本地服务可不保存密钥；未保存时请求不携带 Authorization。':'第三方模型也可使用对应环境变量密钥作为替代来源。'}}</p>
            </el-form-item>
            <el-form-item label="超时（秒）">
              <el-input-number v-model="profileOf(model).timeout_seconds" :min="10" :max="900"/>
            </el-form-item>
            <el-form-item v-if="String(key)==='speech'" label="音色">
              <el-input v-model="profileOf(model).voice" maxlength="100" placeholder="系统音色或本人复刻音色 ID"/>
              <p class="form-help">本人声音复刻完成后，在这里填写该模型对应的音色 ID。</p>
            </el-form-item>
            <el-form-item v-if="String(key)==='speech' && model.source==='third_party' && model.protocol==='dashscope' && model.model?.startsWith('qwen-audio-3.0-tts-')" label="朗读指导">
              <el-input v-model="model.instruction" type="textarea" :rows="3" maxlength="200" placeholder="像给朋友讲科技新闻，自然、有起伏，关键词轻微重读，句间自然停顿。"/>
            </el-form-item>
            <el-form-item v-if="String(key)==='speech' && model.source==='third_party' && model.protocol==='dashscope'" label="语言类型">
              <el-select v-model="model.language_type" placeholder="由后端配置">
                <el-option label="Chinese" value="Chinese"/>
              </el-select>
            </el-form-item>
            <el-form-item v-if="String(key)==='image'" label="图像尺寸">
              <el-select v-model="profileOf(model).size" allow-create filterable>
                <el-option label="2K（Seedream）" value="2K"/>
                <el-option label="4K（Seedream）" value="4K"/>
                <el-option label="1024 × 1536" value="1024x1536"/>
                <el-option label="1024 × 1024" value="1024x1024"/>
                <el-option label="1536 × 1024" value="1536x1024"/>
              </el-select>
            </el-form-item>
            <el-form-item v-if="String(key)==='image' && model.source==='third_party' && model.protocol==='ark'" label="图片角落“AI生成”字样">
              <el-switch v-model="model.watermark"/>
              <p class="form-help">用于之后生成的图片；关闭不会修改已有图片。平台的 AI 内容声明独立保留。</p>
            </el-form-item>
            <el-form-item v-if="String(key)==='text'" label="最大输出 Token（可留空）">
              <el-input-number v-model="profileOf(model).max_completion_tokens" :min="256" :max="32768" clearable/>
            </el-form-item>
            <el-form-item v-if="String(key)==='text'" label="思考模式（服务商支持时）">
              <el-select v-model="profileOf(model).enable_thinking">
                <el-option label="服务默认，不传此参数" :value="null"/>
                <el-option label="关闭" :value="false"/>
                <el-option label="开启" :value="true"/>
              </el-select>
            </el-form-item>
            <el-form-item v-if="String(key)==='text'" label="结构化 JSON 输出">
              <el-switch v-model="profileOf(model).json_mode"/>
            </el-form-item>
            <el-form-item :label="model.source==='local'?'启用本地调用':'启用 API 调用'">
              <el-switch v-model="profileOf(model).enabled"/>
              <p class="form-help">{{String(key)==='video'?'仅在分镜中确认生成 AI 视频后调用并计费。':model.source==='local'?'启用后，生成内容会调用该本机服务。':'启用后，生成内容或日报可能产生服务商费用。'}}</p>
            </el-form-item>
          </template>
        </el-form>
      </el-card>
    </div>
    <h3>数据采集服务</h3>
    <el-card shadow="never" class="tikhub-card">
      <template #header>TikHub（X 数据采集）</template>
      <el-form label-position="top">
        <el-form-item label="API Key">
          <div class="key-row">
            <el-input
              v-model="tikhubDraft"
              type="password"
              show-password
              :placeholder="tikhubStatus?.key_configured?'已配置密钥；输入新密钥可替换':'输入 TikHub API Key 后点击保存密钥'"
              @keyup.enter="saveTikhubKey('set')"
            />
            <el-button :loading="tikhubSaving" :disabled="!tikhubDraft.trim()" @click="saveTikhubKey('set')">保存密钥</el-button>
            <el-button :disabled="tikhubSaving || tikhubStatus?.key_source!=='keychain'" @click="saveTikhubKey('clear')">清除已保存密钥</el-button>
          </div>
          <el-tag :type="tikhubStatus?.key_source==='keychain'?'success':tikhubStatus?.key_configured?'warning':'info'">{{tikhubKeyText(tikhubStatus)}}</el-tag>
          <p class="form-help">用于「X（TikHub）」账号来源和「X搜索（TikHub）」来源。密钥写入 macOS 钥匙串，不进入设置数据、数据库、日志或备份；保存后下一次采集即生效，无需重启。钥匙串未保存时回退使用 .env 中的 PUBLISHER_TIKHUB_API_KEY。保存密钥不会发起 TikHub 请求或计费。</p>
        </el-form-item>
      </el-form>
    </el-card>
    <h3>视频与额度</h3>
    <el-form label-position="top" class="settings-form">
      <el-form-item label="审核通过后自动发布">
        <el-switch v-model="settings.publishing.auto_publish_after_approval"/>
        <p class="form-help">开启后，新通过审核的内容会自动排队上传并点击最终发布，使用已绑定的平台账号。发布结果不明确时暂停核验，避免重复提交。</p>
      </el-form-item>
      <div class="form-grid">
        <el-form-item label="画面渲染方式">
          <el-select v-model="settings.video.renderer">
            <el-option label="FFmpeg（默认）" value="ffmpeg"/>
            <el-option label="Hypit 动态画面（本地）" value="hypit"/>
          </el-select>
          <p class="form-help">Hypit 使用本地浏览器渲染画面，沿用现有旁白、字幕和审核；需先按安装文档准备工具。</p>
        </el-form-item>
        <el-form-item label="AI 视频片段时长（秒）">
          <el-input-number v-model="settings.video.clip_duration" :min="4" :max="15"/>
        </el-form-item>
        <el-form-item label="AI 视频分辨率">
          <el-select v-model="settings.video.clip_resolution"><el-option value="768P"/><el-option value="2K"/></el-select>
          <p class="form-help">Metaso 参考价：768P 每秒 ¥0.09，2K 每秒 ¥0.15，实际以服务商计费为准。</p>
        </el-form-item>
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
      <el-button type="primary" :loading="settingsSaving" :disabled="!!keySaving" @click="saveSettings">保存设置</el-button>
    </el-form>
  </section>
  <section v-if="tab==='schedules'">
    <div class="section-title">
      <h3>定时计划</h3>
      <el-button type="primary" @click="openSchedule()">新建计划</el-button>
    </div>
    <el-alert type="warning" :closable="false" title="启用计划后会按照每日调用上限执行；流水线计划需要已经配置并启用所需模型，资讯采集计划不调用模型。"/>
    <el-empty v-if="!schedules.length" description="暂无定时计划"/>
    <el-table v-else :data="schedules" class="data-table">
      <el-table-column prop="name" label="名称"/>
      <el-table-column label="类型"><template #default="{row}">{{scheduleKindLabel(row.kind)}}</template></el-table-column>
      <el-table-column label="时间">
        <template #default="{row}">{{row.kind==='collect' ? `每 ${intervalLabel(row.interval_minutes)}${row.include_paid ? '（含付费来源）' : ''}` : row.time}}</template>
      </el-table-column>
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
        <el-radio value="metrics">作品数据采集</el-radio>
        <el-radio value="collect">资讯采集</el-radio>
      </el-radio-group>
      <p v-if="scheduleForm.kind==='collect'" class="form-help">只采集已启用来源的最新资讯并入库，不调用模型、不生成内容；连续失败的来源会自动暂缓，避免反复请求。</p>
    </el-form-item>
    <template v-if="scheduleForm.kind==='collect'">
      <el-form-item label="采集间隔">
        <el-select v-model="scheduleForm.interval_minutes" placeholder="默认每 3 小时">
          <el-option v-for="minutes in intervalOptions" :key="minutes" :label="`每 ${intervalLabel(minutes)}`" :value="minutes"/>
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-checkbox v-model="scheduleForm.include_paid">包含 TikHub 付费来源（每个付费来源每轮计 1 次调用）</el-checkbox>
      </el-form-item>
    </template>
    <template v-else>
      <el-form-item label="每天执行时间">
        <el-time-picker v-model="scheduleForm.time" value-format="HH:mm" format="HH:mm"/>
      </el-form-item>
      <el-form-item label="每日上限">
        <el-input-number v-model="scheduleForm.daily_limit" :min="1" :max="20"/>
      </el-form-item>
    </template>
    <el-form-item label="时区">
      <el-input v-model="scheduleForm.timezone"/>
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

<style scoped>
.key-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;width:100%}
.key-row .el-input{flex:1;min-width:180px}
.tikhub-card{max-width:720px;margin-bottom:8px}
.key-stale{color:#b45309}
.builtin-link{margin-top:12px}
@media(max-width:480px){
  .key-row{flex-direction:column;align-items:stretch}
  .key-row .el-input{min-width:0}
  .key-row .el-button+.el-button{margin-left:0}
}
</style>
