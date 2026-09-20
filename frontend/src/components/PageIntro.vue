<script setup lang="ts">
defineProps<{
    title: string;
    description: string;
}>();
</script>
<template>
<div class="page-intro">
  <div>
    <h2>{{ title }}</h2>
    <p>{{ description }}</p>
  </div>
  <div>
    <slot />
  </div>
</div>
</template>
