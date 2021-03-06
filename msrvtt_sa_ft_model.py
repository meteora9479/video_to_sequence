#-*- coding: utf-8 -*-
import tensorflow as tf
import pandas as pd
from pandas import Series, DataFrame
import numpy as np
import os
#import ipdb
import sys
from tfdot import tfdot
import cv2

from tensorflow.python.ops import rnn_cell
from keras.preprocessing import sequence

class Video_Caption_Generator():
    def __init__(self, dim_image, n_words, dim_hidden, batch_size, n_lstm_steps, drop_out_rate, bias_init_vector=None, restore=False, pre_train=False, pre_model=None ):
        self.dim_image = dim_image
        self.n_words = n_words
        self.dim_hidden = dim_hidden
        self.batch_size = batch_size
        self.n_lstm_steps = n_lstm_steps
        self.drop_out_rate = drop_out_rate

        if pre_train == True or restore == True:
            with tf.device("/gpu:2"):
                self.Wemb = tf.Variable(tf.random_uniform([n_words, dim_hidden], -0.1, 0.1), name='Wemb')

                
            self.lstm1 = rnn_cell.LSTMCell(self.dim_hidden,self.dim_hidden,use_peepholes = True)
            self.lstm1_dropout = rnn_cell.DropoutWrapper(self.lstm1,output_keep_prob=1 - self.drop_out_rate)
            self.lstm2 = rnn_cell.LSTMCell(self.dim_hidden,self.dim_hidden,use_peepholes = True)
            self.lstm2_dropout = rnn_cell.DropoutWrapper(self.lstm2,output_keep_prob=1 - self.drop_out_rate)

            self.encode_image_W = tf.Variable( tf.random_uniform([dim_image, dim_hidden], -0.1, 0.1), name='encode_image_W')
            self.encode_image_b = tf.Variable( tf.zeros([dim_hidden]), name='encode_image_b')

            self.embed_word_W = tf.Variable(tf.random_uniform([dim_hidden, n_words], -0.1,0.1), name='embed_word_W')
            if bias_init_vector is not None:
                self.embed_word_b = tf.Variable(bias_init_vector.astype(np.float32), name='embed_word_b')
            else:
                self.embed_word_b = tf.Variable(tf.zeros([n_words]), name='embed_word_b')
                
            if restore == True:
                self.embed_att_w = tf.Variable(tf.random_uniform([dim_hidden, 1], -0.1,0.1), name='embed_att_w')
                self.embed_att_Wa = tf.Variable(tf.random_uniform([dim_hidden, dim_hidden], -0.1,0.1), name='embed_att_Wa')
                self.embed_att_Ua = tf.Variable(tf.random_uniform([dim_hidden, dim_hidden],-0.1,0.1), name='embed_att_Ua')
                self.embed_att_ba = tf.Variable( tf.zeros([dim_hidden]), name='embed_att_ba')    

        else:
            #soft attention weights      
            self.Wemb = pre_model.Wemb
            self.lstm1 = pre_model.lstm1
            self.lstm1_dropout = pre_model.lstm1_dropout
            self.lstm2 = pre_model.lstm2
            self.lstm2_dropout = pre_model.lstm2_dropout
            self.encode_image_W = pre_model.encode_image_W
            self.encode_image_b = pre_model.encode_image_b
            self.embed_word_W = pre_model.embed_word_W
            self.embed_word_b = pre_model.embed_word_b
            
            #with tf.variable_scope("sa_weight"): 
            self.embed_att_w = tf.Variable(tf.random_uniform([dim_hidden, 1], -0.1,0.1), name='embed_att_w')
            self.embed_att_Wa = tf.Variable(tf.random_uniform([dim_hidden, dim_hidden], -0.1,0.1), name='embed_att_Wa')
            self.embed_att_Ua = tf.Variable(tf.random_uniform([dim_hidden, dim_hidden],-0.1,0.1), name='embed_att_Ua')
            self.embed_att_ba = tf.Variable( tf.zeros([dim_hidden]), name='embed_att_ba')    


    def build_model(self):
        video = tf.placeholder(tf.float32, [self.batch_size, self.n_lstm_steps, self.dim_image], name='video_frames')
        video_mask = tf.placeholder(tf.float32, [self.batch_size, self.n_lstm_steps], name='padding_frames')

        caption = tf.placeholder(tf.int32, [self.batch_size, self.n_lstm_steps])
        caption_mask = tf.placeholder(tf.float32, [self.batch_size, self.n_lstm_steps])

        video_flat = tf.reshape(video, [-1, self.dim_image])
        image_emb = tf.nn.xw_plus_b( video_flat, self.encode_image_W, self.encode_image_b) # (batch_size*n_lstm_steps, dim_hidden)
        image_emb = tf.reshape(image_emb, [self.batch_size, self.n_lstm_steps, self.dim_hidden])

        state1 = tf.zeros([self.batch_size, self.lstm1.state_size])
        state2 = tf.zeros([self.batch_size, self.lstm2.state_size])
        padding = tf.zeros([self.batch_size, self.dim_hidden])

        probs = []
        loss = 0.0
        encode_outputs = []

        with tf.name_scope("encoding_stage"):
            for i in range(self.n_lstm_steps): ## Phase 1 => only read frames
                if i > 0:
                    tf.get_variable_scope().reuse_variables()

                with tf.variable_scope("LSTM1"):
                    #output1, state1 = self.lstm1( image_emb[:,i,:], state1 )
                    output1, state1 = self.lstm1_dropout( image_emb[:,i,:], state1 )
                    if i > 0:
                        encode_outputs = tf.concat(0,[encode_outputs,output1])
                    else:    
                        encode_outputs = output1

                with tf.variable_scope("LSTM2"):
                    #output2, state2 = self.lstm2( tf.concat(1,[padding,output1]), state2 )
                    output2, state2 = self.lstm2_dropout( tf.concat(1,[padding,output1]), state2 )

        # Each video might have different length. Need to mask those.
        # But how? Padding with 0 would be enough?
        # Therefore... TODO: for those short videos, keep the last LSTM hidden and output til the end.

        encode_outputs = tf.reshape(encode_outputs, [self.batch_size, self.n_lstm_steps, self.dim_hidden])
        encode_outputs = tf.transpose(encode_outputs, [1,0,2]) # n x b x h
        brcst_w = tf.tile(tf.expand_dims(self.embed_att_w, 0), [self.n_lstm_steps,1,1]) # n x h x 1
        encode_vector = tf.batch_matmul(encode_outputs, tf.tile(tf.expand_dims(self.embed_att_Ua, 0), [self.n_lstm_steps,1,1])) + self.embed_att_ba # n x b x h
        h_prev = tf.zeros([self.batch_size, self.dim_hidden]) # b x h

        with tf.name_scope("decoding_stage"):
            for i in range(self.n_lstm_steps): ## Phase 2 => only generate captions
                if i == 0:
                    current_embed = tf.zeros([self.batch_size, self.dim_hidden])
                else:
                    with tf.device("/gpu:2"):
                        current_embed = tf.nn.embedding_lookup(self.Wemb, caption[:,i-1])

                e = tf.tanh(tf.matmul(h_prev, self.embed_att_Wa) + encode_vector) # n x b x h
                e = tf.batch_matmul(e, brcst_w)    # unnormalized relevance score 
                e = tf.reduce_sum(e,2) # n x b
                e_hat_exp = tf.mul(tf.transpose(video_mask), tf.exp(e)) # n x b 
                denomin = tf.reduce_sum(e_hat_exp,0) # b
                denomin = denomin + tf.to_float(tf.equal(denomin, 0))   # regularize denominator
                alphas = tf.tile(tf.expand_dims(tf.div(e_hat_exp,denomin),2),[1,1,self.dim_hidden]) # n x b x h  # normalize to obtain alpha
                attention_list = tf.mul(alphas, encode_outputs) # n x b x h
                atten = tf.reduce_sum(attention_list,0) # b x h       #  soft-attention weighted sum

                tf.get_variable_scope().reuse_variables()
                with tf.variable_scope("LSTM1"):
                    #output1, state1 = self.lstm1( padding, state1 )
                    output1, state1 = self.lstm1_dropout( padding, state1 )

                with tf.variable_scope("LSTM2"):
                    #output2, state2 = self.lstm2( tf.concat(1,[current_embed,output1]), state2 )
                    output2, state2 = self.lstm2_dropout( tf.concat(1,[current_embed,atten]), state2 )
                    
                h_prev = output2 # b x h    
                labels = tf.expand_dims(caption[:,i], 1)
                indices = tf.expand_dims(tf.range(0, self.batch_size, 1), 1)
                concated = tf.concat(1, [indices, labels])
                onehot_labels = tf.sparse_to_dense(concated, tf.pack([self.batch_size, self.n_words]), 1.0, 0.0)

                logit_words = tf.nn.xw_plus_b(output2, self.embed_word_W, self.embed_word_b)
                cross_entropy = tf.nn.softmax_cross_entropy_with_logits(logit_words, onehot_labels)
                cross_entropy = cross_entropy * caption_mask[:,i]

                probs.append(logit_words)

                current_loss = tf.reduce_sum(cross_entropy)
                loss += current_loss

        loss = loss / tf.reduce_sum(caption_mask)
        return loss, video, video_mask, caption, caption_mask, probs

############### Global Parameters ###############
video_path = './youtube_videos'
video_data_path='./video_corpus.csv'
video_feat_path = './youtube_feats'

train_val_video_feat_path = '/home2/dataset/MSR-VTT/train_val_feats'
train_val_sents_gt_path = '/home2/dataset/MSR-VTT/train_val_sents_gt.txt'

ft_train_video_feat_path = '/home2/dataset/MSVD/MSVD_train_feats'
ft_train_sents_gt_path = '/home2/dataset/MSVD/train_sents_gt.txt'

model_path = './MSRVTT_sa_ft_models5/'
############## Train Parameters #################
dim_image = 4096
dim_hidden= 256
n_frame_step = 80
n_epochs = 1000
batch_size = 100
learning_rate = 0.001
##################################################

def get_video_data(video_data_path, video_feat_path, train_ratio=0.9):
    video_data = pd.read_csv(video_data_path, sep=',')
    video_data = video_data[video_data['Language'] == 'English']
    video_data['video_path'] = video_data.apply(lambda row: row['VideoID']+'_'+str(row['Start'])+'_'+str(row['End'])+'.avi.npy', axis=1)
    video_data['video_path'] = video_data['video_path'].map(lambda x: os.path.join(video_feat_path, x))
    video_data = video_data[video_data['video_path'].map(lambda x: os.path.exists( x ))]
    video_data = video_data[video_data['Description'].map(lambda x: isinstance(x, str))]

    unique_filenames = video_data['video_path'].unique()
    train_len = int(len(unique_filenames)*train_ratio)

    train_vids = unique_filenames[:train_len]
    test_vids = unique_filenames[train_len:]

    train_data = video_data[video_data['video_path'].map(lambda x: x in train_vids)]
    test_data = video_data[video_data['video_path'].map(lambda x: x in test_vids)]

    return train_data, test_data

def MSRVTT_get_video_data( sents_gt_path, video_feat_path, only_train_data=False ):
    video_path = []
    description = []
    videoID = []
    with open(sents_gt_path) as file :
        for line in file :
            id_sent = line.strip().split('\t')
            id_num = int(id_sent[0].split('vid')[1])
            if only_train_data == False or id_num < 6513 :  
                description.append( ''.join(id_sent[-1:]) ) #list to str
                videoID.append( id_sent[0] )
                video_feat_name = id_sent[0].replace('vid','video')
                video_path.append( os.path.join( video_feat_path, video_feat_name+'.mp4.npy' ) )    
                        
    video_data = DataFrame({'VideoID':videoID, 'Description':description, 'video_path':video_path})
    
    return video_data

def MSVD_get_video_data( sents_gt_path, video_feat_path ):
    video_path = []
    description = []
    videoID = []
    with open(sents_gt_path) as file :
        for line in file :
            id_sent = line.strip().split('\t')
            description.append( ''.join(id_sent[-1:]) ) #list to str
            videoID.append( id_sent[0] )
            video_path.append( os.path.join( video_feat_path, id_sent[0]+'.avi.npy' ) )    
                        
    video_data = DataFrame({'VideoID':videoID, 'Description':description, 'video_path':video_path})
    
    return video_data


def preProBuildWordVocab(sentence_iterator, word_count_threshold=5): # borrowed this function from NeuralTalk
    print 'preprocessing word counts and creating vocab based on word count threshold %d' % (word_count_threshold, )
    word_counts = {}
    nsents = 0
    for sent in sentence_iterator:
        nsents += 1
        for w in sent.lower().split(' '):
           word_counts[w] = word_counts.get(w, 0) + 1

    vocab = [w for w in word_counts if word_counts[w] >= word_count_threshold]
    print 'filtered words from %d to %d' % (len(word_counts), len(vocab))

    ixtoword = {}
    ixtoword[0] = '.'  # period at the end of the sentence. make first dimension be end token
    wordtoix = {}
    wordtoix['#START#'] = 0 # make first vector be the start token
    ix = 1
    for w in vocab:
        wordtoix[w] = ix
        ixtoword[ix] = w
        ix += 1

    word_counts['.'] = nsents
    bias_init_vector = np.array([1.0*word_counts[ixtoword[i]] for i in ixtoword])
    bias_init_vector /= np.sum(bias_init_vector) # normalize to frequencies
    bias_init_vector = np.log(bias_init_vector)
    bias_init_vector -= np.max(bias_init_vector) # shift to nice numeric range
    return wordtoix, ixtoword, bias_init_vector

def sa_fine_tune( pre_trained_var, restore=False, restore_model='' ):
    # data w/o split
    #train_data, _ = get_video_data(video_data_path, video_feat_path, train_ratio=0.9)
    #print(train_data)
    #print(type(train_data))
        
    loss_vector = []
    pre_epochs = 0
    train_data = MSVD_get_video_data( ft_train_sents_gt_path, ft_train_video_feat_path )

    msrvtt_vocab_data = MSRVTT_get_video_data( train_val_sents_gt_path, train_val_video_feat_path, True )
    captions = msrvtt_vocab_data['Description'].values
    captions = map(lambda x: x.replace('.', ''), captions)
    captions = map(lambda x: x.replace(',', ''), captions)    
    wordtoix, ixtoword, bias_init_vector = preProBuildWordVocab(captions, word_count_threshold=10)
     
    if restore == True:
        sa_model = Video_Caption_Generator(
            dim_image=dim_image,
            n_words=len(wordtoix),
            dim_hidden=dim_hidden,
            batch_size=batch_size,
            n_lstm_steps=n_frame_step,
            drop_out_rate = 0.5,
            bias_init_vector=bias_init_vector,
            restore=restore)

        tf_loss, tf_video, tf_video_mask, tf_caption, tf_caption_mask, tf_probs = sa_model.build_model()
        sess = tf.InteractiveSession()
        train_op = tf.train.AdamOptimizer(learning_rate).minimize(tf_loss)
        tf.initialize_all_variables().run()
        pre_epochs = int(os.path.basename( restore_model ).split('-')[1])
        loss_vector = np.load( os.path.join( os.path.dirname(restore_model), 'loss-'+str(pre_epochs)+'.npy' ) ).tolist()
        saver = tf.train.Saver()
        saver.restore(sess, restore_model)
        global model_path
        model_path = os.path.dirname(restore_model)
    else:
        pre_trained_model = Video_Caption_Generator(
                dim_image=dim_image,
                n_words=len(wordtoix),
                dim_hidden=dim_hidden,
                batch_size=batch_size,
                n_lstm_steps=n_frame_step,
                drop_out_rate = 0.5,
                bias_init_vector=bias_init_vector,
                restore=restore,
                pre_train=True,
                pre_model=None)

        pre_train_var = set(tf.all_variables())
        sess = tf.InteractiveSession()        
        saver = tf.train.Saver()
        saver.restore(sess, pre_trained_var)
        
        sa_model = Video_Caption_Generator(
            dim_image=dim_image,
            n_words=len(wordtoix),
            dim_hidden=dim_hidden,
            batch_size=batch_size,
            n_lstm_steps=n_frame_step,
            drop_out_rate = 0.5,
            bias_init_vector=bias_init_vector,
            restore=restore,
            pre_train=False,
            pre_model=pre_trained_model)

        tf_loss, tf_video, tf_video_mask, tf_caption, tf_caption_mask, tf_probs = sa_model.build_model()        
        train_op = tf.train.AdamOptimizer(learning_rate).minimize(tf_loss)    
        sess.run(tf.initialize_variables(set(tf.all_variables()) - pre_train_var))
        saver = tf.train.Saver() #save new variables
     
    writer = tf.train.SummaryWriter( model_path, sess.graph )
        
    for epoch in range(n_epochs+1):
        if restore == True:
            epoch = epoch + pre_epochs
       
        index = list(train_data.index)
        np.random.shuffle(index)
        train_data = train_data.ix[index]

        current_train_data = train_data.groupby('video_path').apply(lambda x: x.irow(np.random.choice(len(x))))
        current_train_data = current_train_data.reset_index(drop=True)
        epoch_loss = 0
        for start,end in zip(
                range(0, len(current_train_data), batch_size),
                range(batch_size, len(current_train_data), batch_size)):

            current_batch = current_train_data[start:end]
            current_videos = current_batch['video_path'].values

            current_feats = np.zeros((batch_size, n_frame_step, dim_image))
            current_feats_vals = map(lambda vid: np.load(vid), current_videos)

            current_video_masks = np.zeros((batch_size, n_frame_step))

            for ind,feat in enumerate(current_feats_vals):
                current_feats[ind][:len(current_feats_vals[ind])] = feat
                current_video_masks[ind][:len(current_feats_vals[ind])] = 1

            current_captions = current_batch['Description'].values
            current_captions = map(lambda x: x.replace('.', ''), current_captions)
            current_captions = map(lambda x: x.replace(',', ''), current_captions)
            current_caption_ind = map(lambda cap: [wordtoix[word] for word in cap.lower().split(' ') if word in wordtoix], current_captions)                        
            current_captions_ = map( lambda sent: [ixtoword[ix] for ix in sent], current_caption_ind )
            
            current_caption_matrix = sequence.pad_sequences(current_caption_ind, padding='post', maxlen=n_frame_step-1)
            current_caption_matrix = np.hstack( [current_caption_matrix, np.zeros( [len(current_caption_matrix),1]) ] ).astype(int)
            current_caption_masks = np.zeros((current_caption_matrix.shape[0], current_caption_matrix.shape[1]))
            nonzeros = np.array( map(lambda x: (x != 0).sum()+1, current_caption_matrix ))

            for ind, row in enumerate(current_caption_masks):
                row[:nonzeros[ind]] = 1

#             probs_val = sess.run(tf_probs, feed_dict={
#                 tf_video:current_feats,
#                 tf_caption: current_caption_matrix
#                 })

            _, loss_val = sess.run(
                    [train_op, tf_loss],
                    feed_dict={
                        tf_video: current_feats,
                        tf_video_mask : current_video_masks,
                        tf_caption: current_caption_matrix,
                        tf_caption_mask: current_caption_masks
                        })
            
            epoch_loss = loss_val
            print loss_val
            
        loss_vector.append( epoch_loss )
        if np.mod(epoch, 100) == 0:
            if restore == False or epoch-pre_epochs != 0:
                print "Epoch ", epoch, " is done. Saving the model ..."
                saver.save(sess, os.path.join(model_path, 'model'), global_step=epoch)
                np.save( os.path.join(model_path, 'loss-'+str(epoch)),loss_vector )

sa_fine_tune( 'MSRVTT_models3/model-800')
#sa_fine_tune( '', True, 'MSRVTT_sa_ft_models1/model-1000' )