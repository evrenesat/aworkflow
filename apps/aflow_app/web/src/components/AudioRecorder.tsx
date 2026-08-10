import { useState, useRef } from 'react'
import * as api from '../api'

interface AudioRecorderProps {
  onTranscript: (text: string) => void
  disabled?: boolean
}

export function AudioRecorder({ onTranscript, disabled }: AudioRecorderProps) {
  const [isRecording, setIsRecording] = useState(false)
  const [isProcessing, setIsProcessing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])

  const startRecording = async () => {
    try {
      setError(null)
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      
      const mediaRecorder = new MediaRecorder(stream, {
        mimeType: 'audio/webm',
      })
      
      mediaRecorderRef.current = mediaRecorder
      chunksRef.current = []

      mediaRecorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          chunksRef.current.push(event.data)
        }
      }

      mediaRecorder.onstop = async () => {
        stream.getTracks().forEach(track => track.stop())
        
        const audioBlob = new Blob(chunksRef.current, { type: 'audio/webm' })
        await uploadAndTranscribe(audioBlob)
      }

      mediaRecorder.start()
      setIsRecording(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start recording')
    }
  }

  const stopRecording = () => {
    if (mediaRecorderRef.current && isRecording) {
      mediaRecorderRef.current.stop()
      setIsRecording(false)
    }
  }

  const uploadAndTranscribe = async (audioBlob: Blob) => {
    setIsProcessing(true)
    setError(null)

    try {
      const data = await api.transcribeAudio(new File([audioBlob], 'recording.webm', { type: 'audio/webm' }))
      onTranscript(data.text)
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to transcribe audio'
      setError(errorMessage)
      
      if (errorMessage.includes('not configured')) {
        setError('Audio transcription is not configured on the server')
      }
    } finally {
      setIsProcessing(false)
    }
  }

  return (
    <div className="audio-recorder">
      <button
        type="button"
        onClick={isRecording ? stopRecording : startRecording}
        disabled={disabled || isProcessing}
        className={`btn ${isRecording ? 'btn-danger' : 'btn-secondary'}`}
        style={{ minWidth: '100px' }}
      >
        {isProcessing ? 'Processing...' : isRecording ? 'Stop' : '🎤 Record'}
      </button>
      
      {error && (
        <div className="text-danger text-sm" style={{ marginTop: 'var(--spacing-sm)' }}>
          {error}
        </div>
      )}
      
      {isRecording && (
        <div className="text-dim text-sm" style={{ marginTop: 'var(--spacing-sm)' }}>
          Recording... Click Stop when done
        </div>
      )}
    </div>
  )
}
