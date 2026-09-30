<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { api } from '../api';
import { ApiError, actionKey, formatDate, platformLabel } from '../lib';
import type { Account, AccountChannel, AccountInput, BrowserActionResult, BrowserStatus, Content, Platform, Publication } from '../types';
import PageIntro from '../components/PageIntro.vue';
import StatePanel from '../components/StatePanel.vue';
import StatusTag from '../components/StatusTag.vue';
const publications = ref<Publication[]>([]);
const accounts = ref<Account[]>([]);
const devices = ref<any[]>([]);
const contents = ref<Content[]>([]);
const automaticPosting = ref(false);
const inspections = ref<Record<string, any>>({});
const browserStatus = ref<BrowserStatus | null>(null);
const browserStatusError = ref('');
const browserResults = ref<Record<string, BrowserActionResult>>({});
const browserPlatforms: Platform[] = ['douyin', 'xiaohongshu'];
const loading = ref(false), error = ref('');
const accountDialog = ref(false), publishDialog = ref(false), confirmDialog = ref(false), detailDrawer = ref(false);
const savingAccount = ref(false), preparing = ref(false), confirming = ref(false);
const pairing = ref(false);
const pairingForm = ref({ ip: '', pairing_port: '', connection_port: '', pairing_code: '' });
const pairedPending = ref(false);
const pairedSerial = ref('');
const qrStarting = ref(false), qrPolling = ref(false), qrImage = ref(''), qrSession = ref(''), qrStatus = ref('');
const qrTimer = ref<number>();
const cancelling = ref<string | null>(null), resuming = ref<string | null>(null), inspecting = ref<string | null>(null), detecting = ref<string | null>(null), browserBusy = ref<string | null>(null);
const selectedPublication = ref<Publication | null>(null), editingAccount = ref<Account | null>(null), confirmTarget = ref<Publication | null>(null);
const timer = ref<number>();
const accountForm = ref<any>({ channel: 'browser', platform: 'douyin', display_name: '', account_identifier: '', device_serial: '' });
const publishForm = ref({ content_id: '', account_id: '' });
const confirmForm = ref({ work_url: '', work_id: '', notes: '', confirmed: false });
const approvedContents = computed(() => contents.value.filter(content => content.status === 'APPROVED'));
const connectedDeviceCount = computed(() => devices.value.filter(device => device.state === 'device').length);
const pendingPublicationCount = computed(() => publications.value.filter(publication => ['WAITING_USER', 'NEEDS_ATTENTION', 'VERIFYING'].includes(publication.status)).length);
const publishedCount = computed(() => publications.value.filter(publication => publication.status === 'PUBLISHED').length);
const chosenContent = computed(() => contents.value.find(content => content.id === publishForm.value.content_id));
const chosenAccount = computed(() => accounts.value.find(account => account.id === publishForm.value.account_id));
const publishChannel = computed(() => channelOf(chosenAccount.value));
const androidAccounts = computed(() => accounts.value.filter(account => channelOf(account) === 'android'));
const inspectionKey = (serial: string, platform: string) => `${serial}:${platform}`;
const evidenceValue = (publication: Publication, key: string) => typeof publication.evidence === 'object' && publication.evidence ? (publication.evidence as any)[key] : undefined;
function channelOf(value?: { channel?: AccountChannel } | null): AccountChannel { return value?.channel === 'browser' ? 'browser' : 'android'; }
const channelLabel = (channel: AccountChannel) => channel === 'browser' ? '浏览器' : '手机';
const handoffText = (publication: Publication) => {
    const browser = channelOf(publication) === 'browser';
    if (publication.status === 'WAITING_USER')
        return browser ? '内容已备好并停在本机 Chrome 创作者页面发布前一步。请核对账号、视频和文案后自行点击发布；发布后打开该作品详情页，工作台可自动识别链接。' : '请在手机编辑器核对账号、视频和文案。发布后打开该作品详情，工作台可自动识别链接。';
    if (publication.status === 'NEEDS_ATTENTION')
        return browser ? '浏览器现场状态需要处理；在本机 Chrome 打开对应平台页面检查后继续准备，或打开已发作品让工作台识别。' : '设备现场状态需要处理；连接绑定手机后继续准备，或打开已发作品让工作台识别。';
    if (publication.status === 'VERIFYING')
        return browser ? '最终发布已提交且不会重复点击；请在本机 Chrome 打开该作品详情，由工作台识别并完成核验。' : '最终发布已提交且不会重复点击；请在手机打开该作品详情，由工作台识别并完成核验。';
    return '当前无需人工接管。';
};
const accountFor = (publication: Publication) => accounts.value.find(account => account.id === publication.account_id);
const hasConnectedDevice = (publication: Publication) => {
    const account = accountFor(publication);
    return !!account && devices.value.some(device => device.serial === account.device_serial && device.state === 'device');
};
const detectDisabled = (publication: Publication) => channelOf(publication) === 'browser' ? !!browserStatus.value && !browserStatus.value.ready : !hasConnectedDevice(publication);
async function load() {
    loading.value = true;
    error.value = '';
    try {
        const [publicationResponse, accountResponse, deviceResponse, contentResponse, settingsResponse] = await Promise.all([api.publications(), api.accounts(), api.devices(), api.contents('', ''), api.settings()]);
        publications.value = publicationResponse.items;
        accounts.value = accountResponse.items;
        devices.value = deviceResponse.items;
        contents.value = contentResponse.items;
        automaticPosting.value = !!settingsResponse.publishing?.auto_publish_after_approval;
        await loadBrowserStatus();
    }
    catch (exception: any) {
        error.value = exception.message;
    }
    finally {
        loading.value = false;
    }
}
async function loadBrowserStatus() {
    try {
        browserStatus.value = await api.browserStatus();
        browserStatusError.value = '';
    }
    catch (exception: any) {
        browserStatus.value = null;
        browserStatusError.value = exception.message;
    }
}
async function pairWirelessDevice() {
    if (pairing.value)
        return;
    const pairingPort = Number(pairingForm.value.pairing_port);
    const connectionPort = pairingForm.value.connection_port ? Number(pairingForm.value.connection_port) : undefined;
    if (!Number.isInteger(pairingPort) || pairingPort < 1 || pairingPort > 65535 || (connectionPort !== undefined && (!Number.isInteger(connectionPort) || connectionPort < 1 || connectionPort > 65535))) {
        ElMessage.error('端口应为 1～65535 的数字');
        return;
    }
    pairing.value = true;
    pairedSerial.value = '';
    pairedPending.value = false;
    try {
        const result = await api.pairWirelessDevice({
            ip: pairingForm.value.ip.trim(),
            pairing_port: pairingPort,
            connection_port: connectionPort === pairingPort ? undefined : connectionPort,
            pairing_code: pairingForm.value.pairing_code,
        });
        if (result.connected && result.serial) {
            pairedSerial.value = result.serial;
            devices.value = (await api.devices()).items;
            ElMessage.success('无线设备已连接');
        } else {
            pairedPending.value = true;
            if (connectionPort === pairingPort)
                pairingForm.value.connection_port = '';
            ElMessage.warning('配对成功；请填写无线调试主页面的连接端口，再点击“连接已配对设备”');
        }
    }
    catch (exception: any) {
        ElMessage.error(wirelessErrorMessage(exception));
    }
    finally {
        pairingForm.value.pairing_code = '';
        pairing.value = false;
    }
}
function wirelessErrorMessage(exception: unknown) {
    if (exception instanceof ApiError && exception.body.code === 'INVALID_REQUEST') {
        const details = exception.body.details as { errors?: { message?: string }[] } | undefined;
        return details?.errors?.[0]?.message || exception.message;
    }
    return exception instanceof Error ? exception.message : '无线调试操作失败';
}
async function connectPairedDevice() {
    if (pairing.value)
        return;
    const connectionPort = Number(pairingForm.value.connection_port);
    if (!Number.isInteger(connectionPort) || connectionPort < 1 || connectionPort > 65535 || connectionPort === Number(pairingForm.value.pairing_port)) {
        ElMessage.error('请填写无线调试主页面的连接端口，它与配对弹窗的端口不同');
        return;
    }
    pairing.value = true;
    try {
        const result = await api.connectWirelessDevice({ ip: pairingForm.value.ip.trim(), connection_port: connectionPort });
        pairedSerial.value = result.serial;
        devices.value = (await api.devices()).items;
        pairedPending.value = false;
        ElMessage.success('无线设备已连接');
    }
    catch (exception: any) {
        ElMessage.error(wirelessErrorMessage(exception));
    }
    finally {
        pairing.value = false;
    }
}
async function startQrPairing() {
    if (qrStarting.value)
        return;
    if (qrTimer.value)
        clearInterval(qrTimer.value);
    qrStarting.value = true;
    qrImage.value = '';
    qrSession.value = '';
    qrStatus.value = '';
    pairedPending.value = false;
    pairedSerial.value = '';
    try {
        const result = await api.startWirelessQr();
        qrImage.value = result.image;
        qrSession.value = result.session_id;
        qrStatus.value = '等待手机扫码…';
        qrTimer.value = window.setInterval(pollQrPairing, 2000);
    }
    catch (exception: any) {
        ElMessage.error(wirelessErrorMessage(exception));
    }
    finally {
        qrStarting.value = false;
    }
}
async function pollQrPairing() {
    if (!qrSession.value || qrPolling.value)
        return;
    const session = qrSession.value;
    qrPolling.value = true;
    try {
        const result = await api.pollWirelessQr(session);
        if (qrSession.value !== session)
            return;
        if (result.status === 'waiting')
            return;
        if (qrTimer.value)
            clearInterval(qrTimer.value);
        qrSession.value = '';
        qrImage.value = '';
        if (result.status === 'connected' && result.serial) {
            pairedSerial.value = result.serial;
            devices.value = (await api.devices()).items;
            qrStatus.value = '扫码配对并连接成功';
            ElMessage.success(qrStatus.value);
        } else if (result.status === 'paired' && result.ip) {
            pairingForm.value.ip = result.ip;
            pairingForm.value.pairing_port = '';
            pairingForm.value.connection_port = '';
            pairingForm.value.pairing_code = '';
            pairedPending.value = true;
            qrStatus.value = '扫码配对成功，未自动发现连接端口';
            ElMessage.warning('请在上方填写无线调试主页面的连接端口，点击“连接已配对设备”');
        } else {
            qrStatus.value = result.message || (result.status === 'expired' ? '二维码已过期，请重新生成' : '扫码配对失败，请重新生成二维码');
            ElMessage.error(qrStatus.value);
        }
    }
    catch (exception: any) {
        if (qrSession.value !== session)
            return;
        if (qrTimer.value)
            clearInterval(qrTimer.value);
        qrSession.value = '';
        qrImage.value = '';
        qrStatus.value = wirelessErrorMessage(exception);
        ElMessage.error(qrStatus.value);
    }
    finally {
        qrPolling.value = false;
    }
}
async function pollPublications() {
    if (confirmDialog.value || publishDialog.value || accountDialog.value || confirming.value || preparing.value || cancelling.value || resuming.value)
        return;
    try {
        publications.value = (await api.publications()).items;
        if (selectedPublication.value)
            selectedPublication.value = publications.value.find(item => item.id === selectedPublication.value?.id) || null;
    }
    catch { /* retain usable state during transient polling failures */ }
}
function openAccount(account?: Account) {
    editingAccount.value = account || null;
    accountForm.value = account ? { channel: channelOf(account), platform: account.platform, display_name: account.display_name, account_identifier: account.account_identifier, device_serial: channelOf(account) === 'browser' ? '' : account.device_serial } : { channel: 'browser', platform: 'douyin', display_name: '', account_identifier: '', device_serial: '' };
    accountDialog.value = true;
}
async function saveAccount() {
    if (savingAccount.value)
        return;
    savingAccount.value = true;
    try {
        const payload: AccountInput = { channel: accountForm.value.channel, platform: accountForm.value.platform, display_name: accountForm.value.display_name, account_identifier: accountForm.value.account_identifier };
        if (payload.channel === 'android')
            payload.device_serial = accountForm.value.device_serial;
        if (editingAccount.value)
            await api.patchAccount(editingAccount.value.id, payload);
        else
            await api.createAccount(payload);
        accountDialog.value = false;
        await load();
        ElMessage.success(editingAccount.value ? '账号已更新' : '账号已保存');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        savingAccount.value = false;
    }
}
async function inspect(serial: string, platform: 'douyin' | 'xiaohongshu') {
    const key = inspectionKey(serial, platform);
    if (inspecting.value)
        return;
    inspecting.value = key;
    try {
        const result: any = await api.inspect(serial, platform, actionKey());
        inspections.value = { ...inspections.value, [key]: result };
        ElMessage[result.ready ? 'success' : 'warning'](result.ready ? `${platform === 'douyin' ? '抖音' : '小红书'}设备已就绪` : result.warnings?.join('；') || '设备尚未就绪');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        inspecting.value = null;
    }
}
async function openBrowser(platform: Platform) {
    if (browserBusy.value)
        return;
    browserBusy.value = `open:${platform}`;
    try {
        const result = await api.openBrowser(platform);
        browserResults.value = { ...browserResults.value, [platform]: result };
        ElMessage[result.ready ? 'success' : 'warning'](result.ready ? `已在本机 Chrome 打开${platformLabel(platform)}创作者页面` : result.warnings?.join('；') || `已在本机 Chrome 打开${platformLabel(platform)}，请完成登录`);
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        browserBusy.value = null;
    }
}
async function inspectBrowser(platform: Platform) {
    if (browserBusy.value)
        return;
    browserBusy.value = `inspect:${platform}`;
    try {
        const result = await api.inspectBrowser(platform);
        browserResults.value = { ...browserResults.value, [platform]: result };
        ElMessage[result.ready ? 'success' : 'warning'](result.ready ? `${platformLabel(platform)}浏览器账号已就绪` : result.warnings?.join('；') || '浏览器账号尚未就绪');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        browserBusy.value = null;
    }
}
async function prepare() {
    if (!chosenContent.value || preparing.value)
        return;
    preparing.value = true;
    try {
        await api.publish({ content_id: chosenContent.value.id, content_version: chosenContent.value.version, account_id: publishForm.value.account_id }, actionKey());
        publishDialog.value = false;
        await load();
        ElMessage.success(publishChannel.value === 'browser' ? '已创建发布准备任务。内容会停在本机 Chrome 创作者页面发布前一步，系统不会代为点击发布。' : '已创建发布准备任务。请在手机端检查最终内容，系统不会代为点击发布。');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        preparing.value = false;
    }
}
function openConfirm(publication: Publication) {
    confirmTarget.value = publication;
    confirmForm.value = { work_url: publication.work_url || '', work_id: publication.work_id || '', notes: '', confirmed: false };
    confirmDialog.value = true;
}
async function confirmPublication() {
    if (!confirmTarget.value || confirming.value)
        return;
    confirming.value = true;
    try {
        await api.confirmPublication(confirmTarget.value.id, { work_url: confirmForm.value.work_url, work_id: confirmForm.value.work_id || undefined, confirmed: true, notes: confirmForm.value.notes || undefined }, actionKey());
        confirmDialog.value = false;
        await load();
        ElMessage.success('已记录人工确认的实际发布作品');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        confirming.value = false;
    }
}
async function detectPublication(publication: Publication) {
    if (detecting.value)
        return;
    const browser = channelOf(publication) === 'browser';
    try {
        await ElMessageBox.confirm(browser ? '请先在 Chrome 创作者账号中打开刚发布的这条作品详情页。工作台会核对已审核标题并读取分享链接，不会再次点击发布。' : '请先在绑定手机上打开刚发布的这条作品详情页。工作台会核对已审核标题并读取分享链接，不会再次点击发布。', browser ? '从浏览器识别已发作品' : '从手机识别已发作品', { confirmButtonText: '开始识别', cancelButtonText: '返回', type: 'info' });
    }
    catch {
        return;
    }
    detecting.value = publication.id;
    try {
        await api.detectPublication(publication.id, actionKey());
        await load();
        ElMessage.success(browser ? '已从 Chrome 作品页识别链接并完成发布记录' : '已从绑定手机识别作品链接并完成发布记录');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        detecting.value = null;
    }
}
async function cancel(publication: Publication) {
    let confirmation: any = {};
    const browser = channelOf(publication) === 'browser';
    try {
        if (evidenceValue(publication, 'submit_started_at')) {
            await ElMessageBox.prompt('请先在平台核实没有发布这条作品，再输入“未发布”解除防重。已经发布的作品请回填链接。', '核验发布结果', { inputPattern: /^未发布$/, inputErrorMessage: '需核实平台结果并输入“未发布”', confirmButtonText: '确认未发布并取消', cancelButtonText: '返回', type: 'warning' });
            confirmation = { confirmed_not_published: true, notes: '用户在平台核验后确认未发布' };
        } else {
            await ElMessageBox.confirm(browser ? '取消只会停止本地准备流程，不会关闭已打开的 Chrome 页面或撤回已发作品。请先自行核验浏览器现场状态。' : '取消只会停止本地准备流程，不会清理手机端已打开的编辑器或撤回已发作品。请先自行核验手机现场状态。', '取消本地流程', { confirmButtonText: '我已核验，停止本地流程', cancelButtonText: '返回', type: 'warning' });
        }
    }
    catch {
        return;
    }
    cancelling.value = publication.id;
    try {
        await api.cancelPublication(publication.id, confirmation, actionKey());
        await load();
        ElMessage.success(browser ? '本地发布流程已取消；浏览器现场状态未被修改。' : '本地发布流程已取消；手机现场状态未被修改。');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        cancelling.value = null;
    }
}
async function resume(publication: Publication) {
    if (resuming.value)
        return;
    resuming.value = publication.id;
    try {
        await api.resumePublication(publication.id, actionKey());
        await load();
        ElMessage.success(channelOf(publication) === 'browser' ? '已继续准备，请在 Chrome 页面按当前状态核验。' : '已继续准备，请在手机端按当前页面核验。');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        resuming.value = null;
    }
}
async function finishDeclaration(publication: Publication) {
    try {
        await ElMessageBox.confirm('请核对手机上的“含 AI 合成内容”已选中。本操作只点击当前声明页的“确认并发布”，不会重新上传。', '补完声明发布', { confirmButtonText: '已核对，确认发布', cancelButtonText: '返回' });
    } catch { return; }
    resuming.value = publication.id;
    try {
        await api.finishDeclaration(publication.id, actionKey());
        await load();
        ElMessage.success('已提交声明确认，请从手机识别实际作品链接');
    } catch (exception: any) { ElMessage.error(exception.message); }
    finally { resuming.value = null; }
}
function openDetail(publication: Publication) { selectedPublication.value = publication; detailDrawer.value = true; }
onMounted(() => { load(); timer.value = window.setInterval(pollPublications, 5000); });
onBeforeUnmount(() => { if (timer.value)
    clearInterval(timer.value); if (qrTimer.value)
    clearInterval(qrTimer.value); });
</script>

<template>
<PageIntro title="发布与设备" :description="automaticPosting?'审核通过后系统会自动上传、点击一次最终发布并读取作品链接；异常结果不会重复提交。':'手动准备会停在手机编辑器或本机 Chrome 创作者页；发布后可由工作台从当前作品页自动识别链接。'">
  <el-button @click="load">刷新</el-button>
  <el-button @click="openAccount()">账号设置</el-button>
  <el-button type="primary" :disabled="!approvedContents.length || !accounts.length" @click="publishDialog=true">
    开始准备
  </el-button>
</PageIntro>
<StatePanel :loading :error @retry="load">
  <el-alert
    v-if="automaticPosting"
    type="success"
    :closable="false"
    title="审核后自动发布已开启：新审核内容会自动排队，成功后无需复制作品链接。"
  />
  <el-alert
    v-if="androidAccounts.length && !devices.length"
    type="warning"
    :closable="false"
    title="已配置手机发布账号，但当前未发现 ADB 手机；请通过 USB 授权或在下方完成无线调试配对。"
  />
  <section class="overview-strip" aria-label="发布概览">
    <article class="metric">
      <span>已配置账号</span>
      <strong>{{ accounts.length }}</strong>
    </article>
    <article class="metric">
      <span>已连接设备</span>
      <strong>{{ connectedDeviceCount }}<small> / {{ devices.length }} 已发现</small></strong>
    </article>
    <article class="metric" :class="{attention:pendingPublicationCount>0}">
      <span>待人工处理</span>
      <strong>{{ pendingPublicationCount }}</strong>
    </article>
    <article class="metric">
      <span>已发布作品</span>
      <strong>{{ publishedCount }}</strong>
    </article>
  </section>
  <section class="device-strip">
    <el-card shadow="never">
      <template #header>已配置账号</template>
      <el-empty v-if="!accounts.length" description="尚未配置账号" :image-size="45" />
      <div
        v-else
        v-for="account in accounts"
        :key="account.id"
        class="line-item"
      >
        <span>
          <b>{{account.display_name}}</b>
          <small>{{platformLabel(account.platform)}} · {{channelLabel(channelOf(account))}} · {{account.account_identifier}}</small>
        </span>
        <div>
          <code>{{channelOf(account)==='browser'?'本机 Chrome':account.device_serial}}</code>
          <el-button link type="primary" @click="openAccount(account)">编辑</el-button>
        </div>
      </div>
    </el-card>
    <el-card shadow="never">
      <template #header>已发现设备</template>
      <div v-if="!devices.length" class="device-guide">
        <b>未发现 ADB 设备</b>
        <ol>
          <li>用 USB 数据线连接手机，并保持屏幕解锁。</li>
          <li>在手机的开发者选项中开启 USB 调试；连接后手机上会弹出授权确认，请在手机上允许。</li>
          <li>授权完成后点击右上角“刷新”重新检测。</li>
        </ol>
        <small>USB 调试授权只能在手机屏幕上确认，浏览器无法代为授权。</small>
      </div>
      <div
        v-else
        v-for="device in devices"
        :key="device.serial"
        class="device-line"
      >
        <div class="line-item">
          <span>
            <b>{{device.serial}}</b>
            <small>{{device.model || '未知设备'}} · {{device.state}}</small>
          </span>
          <div>
            <el-button
              link
              type="primary"
              :loading="inspecting===inspectionKey(device.serial,'douyin')"
              @click="inspect(device.serial,'douyin')"
            >
              检查抖音
            </el-button>
            <el-button
              link
              type="primary"
              :loading="inspecting===inspectionKey(device.serial,'xiaohongshu')"
              @click="inspect(device.serial,'xiaohongshu')"
            >
              检查小红书
            </el-button>
          </div>
        </div>
        <div v-for="platform in ['douyin','xiaohongshu']" :key="platform">
          <div v-if="inspections[inspectionKey(device.serial,platform)]" class="inspection-result">
            <b>{{platform==='douyin'?'抖音':'小红书'}}：{{inspections[inspectionKey(device.serial,platform)].ready?'已就绪':'需处理'}}</b>
            <span>
              版本
              {{inspections[inspectionKey(device.serial,platform)].app_version || '—'}}
              · 账号
              {{inspections[inspectionKey(device.serial,platform)].account_identifier || '—'}}
            </span>
            <small v-if="inspections[inspectionKey(device.serial,platform)].warnings?.length">
              {{inspections[inspectionKey(device.serial,platform)].warnings.join('；')}}
            </small>
          </div>
        </div>
      </div>
      <div class="wireless-pairing">
        <b>手动添加无线调试设备</b>
        <small>手机和电脑连接同一 Wi-Fi。打开手机“开发者选项 → 无线调试 → 使用配对码配对设备”，填写弹窗中的 IP、配对端口和配对码。连接端口可留空自动查找；如需手动填写，应取自无线调试主页面，通常与配对端口不同。提交前保持配对弹窗打开。</small>
        <el-form label-position="top" @submit.prevent="pairWirelessDevice">
          <div class="wireless-pairing-fields">
            <el-form-item label="手机 IP">
              <el-input v-model="pairingForm.ip" placeholder="例如 192.168.1.100" autocomplete="off" />
            </el-form-item>
            <el-form-item label="配对端口（配对弹窗）">
              <el-input v-model="pairingForm.pairing_port" placeholder="例如 37123" inputmode="numeric" autocomplete="off" />
            </el-form-item>
            <el-form-item label="连接端口（选填；无线调试主页面）">
              <el-input v-model="pairingForm.connection_port" placeholder="留空自动查找" inputmode="numeric" autocomplete="off" />
            </el-form-item>
            <el-form-item label="六位配对码">
              <el-input v-model="pairingForm.pairing_code" type="password" maxlength="6" inputmode="numeric" autocomplete="off" />
            </el-form-item>
          </div>
          <el-button native-type="submit" type="primary" :loading="pairing" :disabled="!pairingForm.ip.trim() || !pairingForm.pairing_port || pairingForm.pairing_code.length!==6">配对并连接</el-button>
          <el-button v-if="pairedPending" :loading="pairing" :disabled="!pairingForm.connection_port" @click="connectPairedDevice">连接已配对设备</el-button>
        </el-form>
        <small v-if="pairedPending">手机已配对，但电脑未自动发现连接端口。请关闭配对弹窗，在无线调试主页面查看“IP 地址和端口”，只修改上方的连接端口后重试连接；无需新的配对码。</small>
        <small v-if="pairedSerial">已连接：{{ pairedSerial }}。如账号绑定的设备标识不同，请点击左侧账号“编辑”，填写这个标识。</small>
        <div class="wireless-qr">
          <b>扫描二维码配对</b>
          <small>在手机“开发者选项 → 无线调试 → 使用二维码配对设备”中扫描电脑上的二维码。手机和电脑需在同一 Wi-Fi。</small>
          <el-button :loading="qrStarting" @click="startQrPairing">生成配对二维码</el-button>
          <img v-if="qrImage" :src="qrImage" alt="无线调试配对二维码" />
          <small v-if="qrStatus">{{ qrStatus }}</small>
        </div>
      </div>
    </el-card>
    <el-card shadow="never">
      <template #header>浏览器发布（本机 Chrome）</template>
      <div v-if="browserStatusError" class="device-guide">
        <b>暂时无法读取浏览器运行状态</b>
        <small>{{browserStatusError}}</small>
      </div>
      <template v-else>
        <div class="device-guide">
          <b>{{browserStatus?.ready?'运行环境已就绪':'运行环境未就绪'}}</b>
          <span>browser-use {{browserStatus?.version || '未安装'}} · Chrome {{browserStatus?.chrome_available?'可用':'未找到'}}</span>
          <small v-if="browserStatus?.message">{{browserStatus.message}}</small>
        </div>
        <el-alert
          v-if="browserStatus && !browserStatus.ready"
          type="warning"
          :closable="false"
          title="在终端运行 ./scripts/setup_browser_use.sh 安装本地 browser-use 运行环境（锁定版本，无需模型 Key，不访问云端），并确认本机已安装 Google Chrome。"
        />
        <div v-for="platform in browserPlatforms" :key="platform" class="device-line">
          <div class="line-item">
            <span>
              <b>{{platformLabel(platform)}}</b>
              <small>独立 Chrome 创作者分身</small>
            </span>
            <div>
              <el-button
                link
                type="primary"
                :loading="browserBusy===`open:${platform}`"
                :disabled="!!browserStatus && !browserStatus.ready"
                @click="openBrowser(platform)"
              >
                打开 Chrome 登录
              </el-button>
              <el-button
                link
                type="primary"
                :loading="browserBusy===`inspect:${platform}`"
                :disabled="!!browserStatus && !browserStatus.ready"
                @click="inspectBrowser(platform)"
              >
                检查账号
              </el-button>
            </div>
          </div>
          <div v-if="browserResults[platform]" class="inspection-result">
            <b>{{platformLabel(platform)}}：{{browserResults[platform].ready?'已就绪':'需处理'}}</b>
            <span>账号 {{browserResults[platform].account_identifier || '—'}}</span>
            <small v-if="browserResults[platform].warnings?.length">
              {{browserResults[platform].warnings.join('；')}}
            </small>
          </div>
        </div>
        <small class="muted">登录、滑块与验证码由你在自己的 Chrome 中完成；工作台不保存平台凭据，平台也未对该流程做任何认证或背书。</small>
      </template>
    </el-card>
  </section>
  <section>
    <h3>发布记录</h3>
    <el-empty v-if="!publications.length" description="暂无发布记录" />
    <el-table
      v-else
      :data="publications"
      class="data-table"
      @row-click="openDetail"
    >
      <el-table-column prop="content_title" label="内容" min-width="190" />
      <el-table-column label="平台" width="110">
        <template #default="{row}">{{platformLabel(row.platform)}}</template>
      </el-table-column>
      <el-table-column label="状态" width="130">
        <template #default="{row}">
          <StatusTag :status="row.status" />
        </template>
      </el-table-column>
      <el-table-column label="作品链接" min-width="180">
        <template #default="{row}">
          <a v-if="row.work_url" :href="row.work_url" target="_blank" rel="noopener noreferrer" @click.stop>查看作品</a>
          <span v-else class="muted">尚未确认</span>
        </template>
      </el-table-column>
      <el-table-column label="发布方式" width="110">
        <template #default="{row}">{{row.auto_publish?'审核后自动':channelOf(row)==='browser'?'浏览器确认':'手机确认'}}</template>
      </el-table-column>
      <el-table-column label="更新时间" width="165">
        <template #default="{row}">{{formatDate(row.updated_at)}}</template>
      </el-table-column>
      <el-table-column label="操作" width="330">
        <template #default="{row}">
          <el-button
            v-if="['WAITING_USER','NEEDS_ATTENTION','VERIFYING'].includes(row.status)"
            link
            type="primary"
            :loading="detecting===row.id"
            :disabled="detectDisabled(row)"
            @click.stop="detectPublication(row)"
          >
            {{channelOf(row)==='browser'?'从浏览器识别':'从手机识别'}}
          </el-button>
          <el-button
            v-if="['WAITING_USER','NEEDS_ATTENTION','VERIFYING'].includes(row.status)"
            link
            type="success"
            @click.stop="openConfirm(row)"
          >
            确认已发布
          </el-button>
          <el-button
            v-if="['WAITING_USER','NEEDS_ATTENTION'].includes(row.status) && !evidenceValue(row,'submit_started_at')"
            link
            type="primary"
            :loading="resuming===row.id"
            @click.stop="resume(row)"
          >
            继续准备
          </el-button>
          <el-button
            v-if="row.platform==='xiaohongshu' && row.status==='VERIFYING' && evidenceValue(row,'submit_started_at') && !evidenceValue(row,'final_publish_clicked') && !evidenceValue(row,'declaration_confirm_started_at')"
            link type="primary" :loading="resuming===row.id" @click.stop="finishDeclaration(row)"
          >补完声明发布</el-button>
          <el-button
            v-if="!['PUBLISHED','CANCELED'].includes(row.status)"
            link
            type="danger"
            :loading="cancelling===row.id"
            @click.stop="cancel(row)"
          >
            取消
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
</StatePanel>
<el-dialog v-model="accountDialog" :title="editingAccount?'编辑发布账号':'配置发布账号'" width="480px">
  <el-alert type="info" :closable="false" title="仅保存平台、渠道、账号标识与连接方式；系统不会保存登录密码或密钥。"/>
  <el-form label-position="top">
    <el-form-item label="发布渠道">
      <el-radio-group v-model="accountForm.channel">
        <el-radio value="browser">浏览器（本机 Chrome）</el-radio>
        <el-radio value="android">手机（ADB）</el-radio>
      </el-radio-group>
    </el-form-item>
    <el-form-item label="平台">
      <el-radio-group v-model="accountForm.platform" :disabled="!!editingAccount">
        <el-radio value="douyin">抖音</el-radio>
        <el-radio value="xiaohongshu">小红书</el-radio>
      </el-radio-group>
    </el-form-item>
    <el-form-item label="显示名称">
      <el-input v-model="accountForm.display_name"/>
    </el-form-item>
    <el-form-item label="账号标识">
      <el-input v-model="accountForm.account_identifier"/>
    </el-form-item>
    <el-form-item v-if="accountForm.channel==='android'" label="设备序列号">
      <el-input v-model="accountForm.device_serial"/>
    </el-form-item>
    <el-alert v-else type="info" :closable="false" title="浏览器渠道使用本机 Chrome 创作者分身，无需 ADB 设备；首次使用请在发布中心打开 Chrome 完成登录。"/>
  </el-form>
  <template #footer>
    <el-button :disabled="savingAccount" @click="accountDialog=false">取消</el-button>
    <el-button
      type="primary"
      :loading="savingAccount"
      :disabled="!accountForm.display_name||!accountForm.account_identifier||(accountForm.channel==='android'&&!accountForm.device_serial)"
      @click="saveAccount"
    >
      保存
    </el-button>
  </template>
</el-dialog>
<el-dialog v-model="publishDialog" :title="publishChannel==='browser'?'准备浏览器发布':'准备手机端发布'" width="520px">
  <el-alert v-if="publishChannel==='browser'" type="warning" :closable="false" title="这是手动补充准备入口：内容会停在本机 Chrome 创作者页面发布前一步，不点击最终发布；发布后可在列表中从浏览器识别链接。"/>
  <el-alert v-else type="warning" :closable="false" title="这是手动补充准备入口：系统会停在编辑器，不点击最终发布；发布后可在列表中从手机自动识别链接。"/>
  <el-form label-position="top">
    <el-form-item label="已审核内容">
      <el-select v-model="publishForm.content_id">
        <el-option
          v-for="content in approvedContents"
          :key="content.id"
          :value="content.id"
          :label="`${content.title}（v${content.version}）`"
        />
      </el-select>
    </el-form-item>
    <el-form-item label="发布账号">
      <el-select v-model="publishForm.account_id">
        <el-option
          v-for="account in accounts"
          :key="account.id"
          :value="account.id"
          :label="`${platformLabel(account.platform)} · ${account.display_name}（${channelLabel(channelOf(account))}）`"
        />
      </el-select>
    </el-form-item>
  </el-form>
  <template #footer>
    <el-button :disabled="preparing" @click="publishDialog=false">取消</el-button>
    <el-button
      type="primary"
      :loading="preparing"
      :disabled="!publishForm.content_id||!publishForm.account_id"
      @click="prepare"
    >
      开始准备
    </el-button>
  </template>
</el-dialog>
<el-dialog v-model="confirmDialog" :title="confirmTarget && channelOf(confirmTarget)==='browser'?'确认浏览器端已发布':'确认手机端已发布'" width="540px">
  <el-alert type="warning" :closable="false" :title="confirmTarget && channelOf(confirmTarget)==='browser'?'仅在你已于本机 Chrome 实际完成最终发布后确认。短链接无法解析时，请补充作品 ID。':'仅在你已于手机端实际完成最终发布后确认。短链接无法解析时，请补充作品 ID。'"/>
  <el-form label-position="top">
    <el-form-item label="作品 URL">
      <el-input v-model="confirmForm.work_url" placeholder="https://…"/>
    </el-form-item>
    <el-form-item label="作品 ID（短链接无法解析时补充，可选）">
      <el-input v-model="confirmForm.work_id"/>
    </el-form-item>
    <el-form-item label="说明（可选）">
      <el-input v-model="confirmForm.notes" type="textarea" :rows="3"/>
    </el-form-item>
    <el-checkbox v-model="confirmForm.confirmed">{{confirmTarget && channelOf(confirmTarget)==='browser'?'我确认已在本机 Chrome 实际发布此作品':'我确认已在手机端实际发布此作品'}}</el-checkbox>
  </el-form>
  <template #footer>
    <el-button :disabled="confirming" @click="confirmDialog=false">返回</el-button>
    <el-button
      type="success"
      :loading="confirming"
      :disabled="!confirmForm.work_url || !confirmForm.confirmed"
      @click="confirmPublication"
    >
      记录确认
    </el-button>
  </template>
</el-dialog>
<el-drawer v-model="detailDrawer" title="发布详情" size="460px">
  <template v-if="selectedPublication">
    <div class="publication-detail">
      <div>
        <StatusTag :status="selectedPublication.status"/>
        <b>{{selectedPublication.content_title}}</b>
      </div>
      <p>
        <b>发布渠道：</b>
        {{channelOf(selectedPublication)==='browser'?'浏览器（本机 Chrome）':'手机（ADB）'}}
      </p>
      <p>
        <b>当前步骤：</b>
        {{(selectedPublication as any).current_step || evidenceValue(selectedPublication,'current_step') || '等待服务端更新'}}
      </p>
      <p>
        <b>接管指引：</b>
        {{handoffText(selectedPublication)}}
      </p>
      <p v-if="evidenceValue(selectedPublication,'reason')">
        <b>原因：</b>
        {{evidenceValue(selectedPublication,'reason')}}
      </p>
      <p v-if="evidenceValue(selectedPublication,'error_code')">
        <b>错误码：</b>
        {{evidenceValue(selectedPublication,'error_code')}}
      </p>
      <pre v-if="selectedPublication.evidence">{{JSON.stringify(selectedPublication.evidence,null,2)}}</pre>
    </div>
  </template>
</el-drawer>
</template>
