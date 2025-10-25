

<script setup>
import { ref, onMounted } from 'vue'

const characters = ref([])
const loading = ref(false)
const error = ref(null)
const selectedCharacter = ref(null)

const BACKEND_URL = 'http://localhost:9537'

async function fetchCharacters() {
  loading.value = true
  error.value = null
  try {
    const res = await fetch(`${BACKEND_URL}/characters`)
    if (!res.ok) throw new Error('Failed to fetch characters')
    characters.value = await res.json()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

onMounted(fetchCharacters)
</script>

<template>
  <v-app>
    <div class="file-manager">
      <aside class="sidebar">
        <div class="sidebar-title">Characters</div>
        <div v-if="loading" class="sidebar-loading">Loading...</div>
        <div v-if="error" class="sidebar-error">{{ error }}</div>
        <div
          v-for="char in characters"
          :key="char.id"
          :class="['sidebar-item', { active: selectedCharacter === char.id }]"
          @click="selectedCharacter = char.id"
        >
          {{ char.name }}
        </div>
      </aside>
      <main class="main-area">
        <div class="main-content">
          <div class="empty-state">Select a character to view images.</div>
        </div>
      </main>
    </div>
  </v-app>
</template>


<style scoped>
/* Original simple file manager layout */
.file-manager {
  display: flex;
  flex-direction: row;
  position: fixed;
  inset: 0;
  background: #fff;
  min-width: 0;
  min-height: 0;
  box-sizing: border-box;
}
.sidebar {
  width: 220px;
  background: #fff;
  border-right: 1px solid #eee;
  padding: 16px 0 16px 0;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  min-height: 100vh;
}
.sidebar-title {
  font-size: 1.25rem;
  font-weight: 600;
  margin-bottom: 12px;
  padding-left: 16px;
}
.sidebar-item {
  padding: 8px 16px;
  cursor: pointer;
  border-radius: 4px;
  margin-bottom: 4px;
  transition: background 0.2s;
}
.sidebar-item.active, .sidebar-item:hover {
  background: #f0f0f0;
}
.sidebar-loading, .sidebar-error {
  padding: 8px 16px;
  color: #888;
}
.main-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: #fff;
  min-width: 0;
  min-height: 100vh;
  box-sizing: border-box;
  padding: 16px;
}
.main-content {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
.empty-state {
  color: #aaa;
  font-size: 1.2rem;
}
</style>
