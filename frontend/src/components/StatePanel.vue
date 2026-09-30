<script setup lang="ts">
import { Loading } from '@element-plus/icons-vue';
defineProps<{
    loading?: boolean;
    error?: string;
    empty?: boolean;
    emptyText?: string;
}>();
const emit = defineEmits<{
    retry: [
    ];
}>();
</script>
<template>
<div v-if="loading || error || empty" class="state-panel">
  <el-icon v-if="loading" class="is-loading">
    <Loading />
  </el-icon>
  <el-result
    v-else-if="error"
    icon="error"
    title="暂时无法获取数据"
    :sub-title="error"
  >
    <template #extra>
      <el-button @click="emit('retry')">重新获取</el-button>
    </template>
  </el-result>
  <el-empty v-else :description="emptyText || '暂无数据'" :image-size="72" />
</div>
<slot v-else />
</template>
